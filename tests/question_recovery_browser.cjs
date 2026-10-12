// 验证真实页面与课堂服务在连续追问漏登记后继续学习，不出现手动重试或异常发言。
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');

async function main() {
  const reserve = net.createServer();
  await new Promise((resolve,reject) => {reserve.once('error',reject); reserve.listen(0,'127.0.0.1',resolve);});
  const port = reserve.address().port;
  await new Promise(resolve => reserve.close(resolve));
  const root = path.resolve(__dirname,'..');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(),'lfy-recovery-'));
  const python = process.env.LFY_TEST_PYTHON || path.join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  const server = spawn(python,[path.join(__dirname,'question_recovery_server.py'),'--port',String(port),'--root',temporary],
    {cwd:root,stdio:['ignore','pipe','pipe'],env:{...process.env,DEEPSEEK_API_KEY:'',Deepseek_API:''}});
  let stderr = '', browser;
  server.stderr.on('data',data => stderr += data);
  try {
    const base = `http://127.0.0.1:${port}`;
    let ready = false;
    for (let i=0;i<100;i++) {
      if (server.exitCode !== null) throw new Error(stderr);
      try {if ((await fetch(base)).ok) {ready=true;break;}} catch {}
      await new Promise(resolve => setTimeout(resolve,100));
    }
    assert(ready,stderr || '测试服务未启动');
    browser = await chromium.launch({headless:true,...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
    const page = await browser.newPage();
    const errors=[]; page.on('pageerror',error => errors.push(error.message));
    await page.goto(base);
    await page.locator('#topic').fill('一元一次方程');
    await page.locator('#studentProfile').fill('知道基本加减法，尚未学习方程变形。');
    await page.locator('#tts').uncheck(); await page.locator('#asrReview').uncheck();
    await page.locator('#start').click();
    await page.waitForFunction(() => running);
    await page.locator('.text-test summary').click();
    await page.locator('#teacherText').fill('2(x+3)=4x-6，去括号后2x+6=4x-6，再移项得到12=2x，所以x=6。');
    await page.locator('#sendText').click();
    await page.waitForFunction(() => records.some(e => e.type === 'student_recovery'));
    assert.equal(await page.locator('#stateVersion').innerText(),'1');
    assert(await page.locator('#error').isHidden()); assert(await page.locator('#retry').isHidden());
    assert(await page.locator('#sendText').isEnabled());
    const first = await page.evaluate(() => records.filter(e => e.type === 'llm_request' && e.data.phase === 'student'));
    assert.equal(first.length,2);
    assert(first[1].data.body.messages.at(-1).content.includes('question_updates'));
    assert((await page.locator('#events').innerText()).includes('异常追问已跳过，课堂继续'));
    assert(!await page.evaluate(() => records.some(e => e.type === 'reply' || e.type === 'error')));
    await page.locator('#teacherText').fill('继续补充：移项是等式两边同时加减同一个数。');
    await page.locator('#sendText').click();
    await page.waitForFunction(() => document.getElementById('stateVersion').textContent === '2');
    assert.equal(await page.evaluate(() => records.filter(e => e.type === 'llm_request' && e.data.phase === 'student').length),3);
    assert(!await page.evaluate(() => records.some(e => e.type === 'error' || e.type === 'reply')));
    await page.locator('[data-tab=output]').click();
    await page.locator('.debug').screenshot({path:path.join(temporary,'question-recovery.png')});
    assert.deepEqual(errors,[]);
    console.log('PASS real browser → offline classroom: one repair, skipped question, no retry, next lesson continues');
    console.log('Screenshot: '+path.join(temporary,'question-recovery.png'));
  } finally {
    if (browser) await browser.close();
    const exited = new Promise(resolve => {if(server.exitCode !== null) resolve(); else server.once('exit',resolve);});
    server.kill(); await exited;
  }
}
main().catch(error => {console.error(error);process.exitCode=1;});
