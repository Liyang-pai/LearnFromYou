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
  const server = spawn(python,[path.join(__dirname,'question_release_server.py'),'--port',String(port),'--root',temporary],
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
    await page.locator('#topic').fill('盒子规则');
    await page.locator('#studentProfile').fill('知道整数与加法，未学折光。');
    await page.locator('#tts').uncheck(); await page.locator('#asrReview').uncheck();
    await page.locator('#start').click();
    await page.waitForFunction(() => running);
    assert((await page.locator('#runtimeVersion').innerText()).includes('运行版本'));
    const health = await (await fetch(base + '/api/health')).json();
    const readyRuntime = await page.evaluate(() => records.find(e => e.type === 'ready').data.runtime);
    assert.deepEqual(readyRuntime, health.runtime);
    await page.locator('.text-test summary').click();
    async function teach(text, expectedReplies) {
      const states = await page.evaluate(() => records.filter(e => e.type === 'state').length);
      await page.locator('#teacherText').fill(text); await page.locator('#sendText').click();
      await page.waitForFunction(n => records.filter(e => e.type === 'state').length > n, states);
      if (expectedReplies !== null) await page.waitForFunction(n => records.filter(e => e.type === 'reply').length === n, expectedReplies);
    }
    await teach('盒子先折光再加2。', 1);
    await teach('折光的定义还没有解释。', 2);
    await teach('暂不解释，继续讲。', null);
    assert.equal(await page.evaluate(() => records.filter(e => e.type === 'reply').length),2);
    await teach('还有什么疑问吗？', 3);
    let latest = await page.evaluate(() => records.filter(e => e.type === 'reply').at(-1).data);
    assert.equal(latest.kind,'question'); assert.equal(latest.question_id,'q1');
    assert.equal(latest.state.open_questions[0].attempts,3);
    assert.equal(latest.state.open_questions[0].status,'deferred');
    assert(latest.question_release_request);
    assert(!(await page.locator('#conversation').innerText()).includes('教师本次明确邀请说明疑问'));
    await teach('听不到你说话，请确认你在听。', 4);
    latest = await page.evaluate(() => records.filter(e => e.type === 'reply').at(-1).data);
    assert.equal(latest.kind,'ack');
    await page.locator('#mute').check();
    await teach('还有什么疑问吗？', null);
    assert.equal(await page.evaluate(() => records.filter(e => e.type === 'reply').length),4);
    await page.locator('#mute').uncheck();
    await page.waitForFunction(() => records.filter(e => e.type === 'reply').length === 5);
    latest = await page.evaluate(() => records.filter(e => e.type === 'reply').at(-1).data);
    assert.equal(latest.kind,'question'); assert.equal(latest.state.open_questions[0].attempts,4);
    const stages = await page.evaluate(() => records.filter(e => e.type === 'question_release').map(e => e.data.status));
    for (const status of ['requested','approved','delivered']) assert(stages.includes(status));
    const requests = await page.evaluate(() => records.filter(e => e.type === 'llm_request'));
    assert(requests.every(e => e.data.runtime.rules_fingerprint === health.runtime.rules_fingerprint));
    assert(requests.every(e => typeof e.data.prompt_fingerprint === 'string'));
    assert(await page.locator('#error').isHidden());
    await page.locator('[data-tab=output]').click();
    assert((await page.locator('#events').innerText()).includes('已实际发出'));
    await page.locator('.debug').screenshot({path:path.join(temporary,'question-release.png')});
    await page.evaluate(() => showRuntime(null));
    assert((await page.locator('#runtimeVersion').innerText()).includes('请重启后台'));
    assert.deepEqual(errors,[]);
    console.log('PASS real browser: two-attempt cap, invited question release, audio ack, mute resume, runtime identity and old backend notice');
    console.log('Screenshot: '+path.join(temporary,'question-release.png'));
  } finally {
    if (browser) await browser.close();
    const exited = new Promise(resolve => {if(server.exitCode !== null) resolve(); else server.once('exit',resolve);});
    server.kill(); await exited;
  }
}
main().catch(error => {console.error(error);process.exitCode=1;});
