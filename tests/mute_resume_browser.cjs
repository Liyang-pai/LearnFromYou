// 真实 Chrome 页面 + 本机课堂服务；所有模型回复来自内存，不调用外网。
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
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(),'lfy-mute-resume-'));
  const python = process.env.LFY_TEST_PYTHON || path.join(root, '.venv/bin/python');
  const server = spawn(python,[path.join(__dirname,'mute_resume_server.py'),'--port',String(port),'--root',temporary],
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
    await page.locator('#topic').fill('规则A');
    await page.locator('#studentProfile').fill('已有知识：知道整数加法。');
    await page.locator('#mute').check();
    await page.locator('#tts').uncheck(); await page.locator('#asrReview').uncheck();
    await page.locator('#start').click();
    await page.waitForFunction(() => running);
    await page.locator('.text-test summary').click();
    for (const [index,text] of ['请回答第一个问题。','请回答第二个问题。','第一个问题不用回答了，请接着听。'].entries()) {
      await page.locator('#teacherText').fill(text);
      await page.locator('#sendText').click();
      await page.waitForFunction(version => Number(document.getElementById('stateVersion').textContent) === version,index+1);
    }
    assert(!await page.evaluate(() => records.some(e => e.type === 'reply')));
    const muted = await page.evaluate(() => records.filter(e => e.type === 'llm_request' && e.data.phase === 'student')
      .map(e => JSON.parse(e.data.body.messages.findLast(m => m.role === 'user').content).interaction_context.muted));
    assert.deepEqual(muted,[true,true,true]);
    await page.locator('#mute').uncheck();
    await page.waitForFunction(() => records.some(e => e.type === 'reply'));
    const result = await page.evaluate(() => ({
      replies:records.filter(e => e.type === 'reply'),
      resume:records.filter(e => e.type === 'llm_request' && e.data.phase === 'student_resume'),
      state:records.filter(e => e.type === 'reply').at(-1).data.state
    }));
    assert.equal(result.replies.length,1);
    assert.equal(result.resume.length,1);
    assert.equal(result.state.response_requests.find(r => r.id === 'r1').status,'dismissed');
    assert.equal(result.state.response_requests.find(r => r.id === 'r2').status,'answered');
    assert(result.replies[0].data.text.includes('第二个问题'));
    await page.locator('[data-tab=state]').click();
    assert((await page.locator('#stateCards').innerText()).includes('教师待回应请求'));
    await page.locator('.debug').screenshot({path:path.join(temporary,'mute-resume.png')});
    assert.deepEqual(errors,[]);
    console.log('PASS real browser → local classroom: mute learning, dismissal, resume once, visible states');
    console.log('Screenshot: '+path.join(temporary,'mute-resume.png'));
  } finally {
    if (browser) await browser.close();
    const exited = new Promise(resolve => {if(server.exitCode !== null) resolve(); else server.once('exit',resolve);});
    server.kill(); await exited;
  }
}
main().catch(error => {console.error(error);process.exitCode=1;});
