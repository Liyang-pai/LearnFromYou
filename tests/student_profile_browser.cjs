// 用真实浏览器和受控服务验证画像表单、覆盖保护、创建参数及桌面/窄屏布局。
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright');

const frontend = path.resolve(__dirname, '../frontend');
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const name = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/static\//, '');
  const file = path.resolve(frontend, name);
  if (!file.startsWith(frontend + path.sep) || !fs.existsSync(file)) {res.writeHead(404); res.end(); return;}
  res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});

async function main() {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors = [], starts = [], calls = [];
  let asrReady = false, mode = 'success', release, arrived;
  const result = {grade:'成人初学者', subject:'电路基础', points:'电流、电压及其关系',
    student_profile:'已有知识：知道电流表示电荷的定向移动。\n理解基础：能理解简单的比例关系。\n知识边界：尚未学习欧姆定律。\n可能的理解困难：需要具体例子帮助理解电阻。'};
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/health', route => route.fulfill({json:{model_configured:true, asr_debug_available:true}}));
  await page.route('**/api/asr/models', route => route.fulfill({json:{asr_ready:asrReady, busy:false,
    models:[], models_dir:'测试模型目录', engine_state:'idle', vad_ready:true}}));
  await page.route('**/api/student-profile', async route => {
    calls.push(route.request().postDataJSON());
    const requestMode = mode;
    if (requestMode === 'slow') {arrived(); await new Promise(resolve => release = resolve);}
    if (requestMode === 'failure') return route.fulfill({status:502,json:{detail:'模拟生成失败'}});
    if (requestMode === 'invalid') return route.fulfill({json:{...result,student_profile:' '}});
    if (requestMode === 'network') return route.abort('failed');
    if (requestMode === 'timeout') await new Promise(resolve => setTimeout(resolve,100));
    await route.fulfill({json:result});
  });
  await page.routeWebSocket('**/ws/session', ws => ws.onMessage(raw => {
    const message = JSON.parse(raw);
    if (message.type === 'start') {
      starts.push(message);
      ws.send(JSON.stringify({type:'ready',data:{session_id:'012345abcdef',asr_review:true,
        scope:{scope:['欧姆定律'],boundary_note:'待学范围'},state:{version:0,knowledge:[],open_questions:[],recent_events:[]}}}));
    }
  }));
  const button = page.locator('#generateProfile');
  const profile = page.locator('#studentProfile');
  const status = page.locator('#profileStatus');
  const base = `http://127.0.0.1:${server.address().port}`;
  async function idle() {await page.waitForFunction(() => !window.studentProfile.busy);}
  async function confirm() {await page.getByRole('button',{name:'放弃更改并生成',exact:true}).click();}
  async function snapshot() {
    return page.evaluate(() => [...document.querySelectorAll('#lessonForm input,#lessonForm textarea,#lessonForm select')]
      .map(el => ({id:el.id,name:el.name,value:el.value,checked:el.checked})));
  }
  try {
    await page.goto(base);
    await page.waitForFunction(() => !!window.studentProfile);
    assert(await button.isDisabled());
    await page.locator('#topic').fill('欧姆定律');
    assert(await button.isEnabled());
    assert(await page.locator('#start').isDisabled());
    await button.click(); await idle();
    assert.equal(calls.length,1);
    assert.equal(calls[0].student_level,'normal'); assert.equal(calls[0].lesson_type,'new');
    assert.equal(await profile.inputValue(),result.student_profile);
    assert.equal(await page.locator('#grade').inputValue(),'__custom__');
    assert.equal(await page.locator('#gradeCustom').inputValue(),result.grade);
    console.log('PASS topic-only generation without ASR, defaults, inferred custom choices');

    await profile.fill('手写画像：只知道基本加法。');
    await page.locator('#points').fill('  教师填写的知识点\n保留换行  ');
    await page.locator('#grade').selectOption('高一');
    await page.locator('#subject').selectOption('物理');
    await page.locator('[name=studentLevel][value=weak]').check();
    await page.locator('[name=lessonType][value=review]').check();
    await page.locator('.profile-more summary').click();
    await page.locator('#textbook').fill('自编教材');
    const original = await snapshot();
    await button.click();
    await page.getByRole('button',{name:'取消',exact:true}).click(); await idle();
    assert.deepEqual(await snapshot(),original); assert.equal(calls.length,1);
    await button.click(); await page.keyboard.press('Escape'); await idle();
    assert.deepEqual(await snapshot(),original); assert.equal(calls.length,1);
    for (const failure of ['failure','invalid','network','timeout']) {
      if (failure === 'timeout') await page.evaluate(() => {
        window.profileOriginalTimeout = window.setTimeout;
        window.setTimeout = (fn,ms,...args) => window.profileOriginalTimeout(fn,ms === 95000 ? 10 : ms,...args);
      });
      mode = failure; await button.click(); await confirm(); await idle();
      assert((await status.innerText()).includes('原有内容已保留'));
      assert.deepEqual(await snapshot(),original);
      assert(await page.locator('#retry').isHidden());
      if (failure === 'timeout') await page.evaluate(() => {window.setTimeout = window.profileOriginalTimeout;});
    }
    console.log('PASS cancel, Escape, server failure and invalid response retain every field');

    mode = 'slow'; const pending = new Promise(resolve => arrived = resolve);
    await button.click(); await confirm(); await pending;
    assert(await profile.isDisabled()); assert(await page.locator('#points').isDisabled());
    assert(await page.locator('#start').isDisabled());
    const before = calls.length;
    await page.evaluate(() => {document.getElementById('generateProfile').click(); document.getElementById('lessonForm').requestSubmit();});
    assert.equal(calls.length,before); assert.equal(starts.length,0);
    release(); await idle(); mode = 'success';
    assert.equal(await page.locator('#points').inputValue(),'  教师填写的知识点\n保留换行  ');
    assert.equal(await page.locator('#grade').inputValue(),'高一');
    assert.equal(await page.locator('#subject').inputValue(),'物理');
    assert.equal(await profile.inputValue(),result.student_profile);
    assert.equal(calls.at(-1).textbook,'自编教材');
    assert.equal(calls.at(-1).student_level,'weak');
    console.log('PASS pending locks, no duplicate request, teacher constraints preserved');

    asrReady = true;
    await page.waitForFunction(() => !document.getElementById('start').disabled);
    await profile.fill(' \n '); await page.locator('#start').click();
    assert.equal(await status.innerText(),'请生成或填写学生设定，再创建试讲嗷~');
    assert.equal(starts.length,0);
    await profile.fill('已有知识：知道整数加法。\n知识边界：尚未学习欧姆定律。');
    await page.locator('#subject').selectOption('__custom__');
    await page.locator('#subjectCustom').fill(''); await button.click();
    assert.equal(calls.length,before);
    await page.locator('#subjectCustom').fill('电路基础');
    const output = process.env.PROFILE_SCREENSHOT_DIR || os.tmpdir();
    fs.mkdirSync(output,{recursive:true});
    await page.locator('.setup').screenshot({path:path.join(output,'student-profile-desktop.png')});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator('.setup').screenshot({path:path.join(output,'student-profile-mobile.png')});
    await button.click();
    assert(await page.locator('#profileOverwrite').isVisible());
    assert(await page.locator('#profileOverwrite').evaluate(el => el.getBoundingClientRect().right <= innerWidth));
    await page.getByRole('button',{name:'取消',exact:true}).click(); await idle();
    await page.locator('#start').click();
    await page.waitForFunction(() => running);
    assert.equal(starts.length,1);
    assert.equal(starts[0].lesson.subject,'电路基础');
    assert(starts[0].lesson.student_profile.includes('整数加法'));
    assert(!('prerequisites' in starts[0].lesson));
    assert(await profile.isDisabled()); assert(await button.isDisabled());
    // 可复用真实API已经生成的结果验证回填，不再产生模型调用。
    if (process.env.PROFILE_LIVE_RESULTS) {
      const files = process.env.PROFILE_LIVE_RESULTS.split(path.delimiter);
      for (const file of files) {
        const samples = JSON.parse(fs.readFileSync(file, 'utf8')).cases;
        for (const sample of samples.filter(item => item.status === 'completed' && item.id.startsWith('grade-'))) {
          Object.assign(result, sample.profile);
          await page.goto(base);
          await page.waitForFunction(() => !!window.studentProfile);
          await page.locator('#topic').fill(sample.settings.topic);
          if (sample.settings.grade) {
            const standard = await page.locator('#grade option').evaluateAll(options => options.map(option => option.value));
            await page.locator('#grade').selectOption(standard.includes(sample.settings.grade) ? sample.settings.grade : '__custom__');
            if (!standard.includes(sample.settings.grade)) await page.locator('#gradeCustom').fill(sample.settings.grade);
          }
          await button.click(); await idle();
          const choice = page.locator('#grade');
          const standard = await choice.locator('option').evaluateAll(options => options.map(option => option.value));
          assert.equal(await choice.inputValue(), standard.includes(result.grade) ? result.grade : '__custom__');
          if (standard.includes(result.grade)) {
            assert(await page.locator('#gradeCustom').isHidden());
            if (result.grade === '一年级') {
              assert.equal(await choice.locator('option:checked').evaluate(option => option.parentElement.label), '小学');
            }
          } else {
            assert.equal(await page.locator('#gradeCustom').inputValue(), result.grade);
          }
          assert.equal(await profile.inputValue(), result.student_profile);
          console.log('PASS real API result refill', sample.id, result.grade);
        }
      }
    }
    assert.deepEqual(errors,[]);
    console.log('PASS blank-profile block, manual creation, classroom lock and responsive layout');
    console.log('Screenshots: '+output+'/student-profile-{desktop,mobile}.png');
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
}
main().catch(error => {console.error(error); server.close(); process.exitCode=1;});
