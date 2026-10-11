'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const port = Number(process.env.REPORT_TEST_PORT || 8877);
const base = `http://127.0.0.1:${port}`;
const python = process.env.LFY_TEST_PYTHON || path.join(root, '.venv/Scripts/python.exe');
const sid = '012345abcdef';
let report;

(async () => {
  // Seed only a synthetic class in this test checkout; export uses the real endpoint.
  const serverCode = `import asyncio, runpy, uvicorn\nfrom pathlib import Path\nfrom backend.teaching_report import ReportService\nfixtures = runpy.run_path('tests/test_teaching_report.py')\nfixtures['save_classroom'](Path.cwd())\nservice = ReportService(lambda emit: fixtures['FakeModel'](emit, []))\nasync def seed():\n    await service.start('${sid}')\n    task = service.tasks.get('${sid}')\n    if task:\n        await task\nasyncio.run(seed())\nuvicorn.run('backend.app:app', host='127.0.0.1', port=${port})`;
  const server = spawn(python, ['-c', serverCode],
    {cwd: root, windowsHide: true, stdio: 'pipe', env: {...process.env, DEEPSEEK_API_KEY: '', Deepseek_API: '', PORT: String(port)}});
  let serverOutput = ''; server.stderr.on('data', data => serverOutput += data);
  let browser;
  try {
    let ready = false;
    for (let i = 0; i < 100; i++) {
      if (server.exitCode !== null) throw new Error(serverOutput);
      try { if ((await fetch(base)).ok) { ready = true; break; } } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert(ready, 'test server did not start');
    report = (await (await fetch(`${base}/api/teaching-report/${sid}`)).json()).report;
    assert(report?.dimensions, 'v2 seed was not generated');
    browser = await chromium.launch({executablePath: process.env.CHROME_PATH, headless: true});
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    let status = 'generating', posts = 0, fail = false, delayed;
    const response = id => ({schema_version: 2, session_id: id, status, progress: '正在核对全课解释、更正与优先改进……', facts_text: '教师讲授 1 段；未记录到 AI 学生回答，不能据此判断学生是否理解。',
      report: status === 'ready' ? report : null, error: status === 'failed' ? '模拟超时，可以重试。' : '',
      raw_records: Array(10000).fill('原文哨兵不可显示')});
    await page.route('**/api/teaching-report/**', async route => {
      const url = new URL(route.request().url()), id = url.pathname.split('/')[3];
      if (url.pathname.endsWith('/export')) return route.continue();
      if (delayed && id === sid) await delayed.promise;
      if (fail) return route.fulfill({status: 503, json: {detail: '模拟读取失败'}});
      if (route.request().method() === 'POST') { posts++; status = 'generating'; }
      await route.fulfill({json: response(id)});
    });
    await page.goto(base);
    await page.waitForFunction(() => Boolean(window.teachingReport));
    await page.evaluate(id => handle({type: 'finished', data: {session_id: id, unprocessed_sources: [],
      state: {version: 1, knowledge: [], open_questions: [], recent_events: []}}}), sid);
    await page.getByText('正在核对全课解释、更正与优先改进……', {exact: true}).waitFor();
    assert.equal(posts, 0, 'backend owns automatic generation; frontend only reads');
    assert(await page.locator('#exportTeachingReport').isDisabled());
    assert(await page.locator('#generateTeachingReport').isHidden());
    status = 'ready';
    await page.getByText('教学报告已生成，可以查看和导出。', {exact: true}).waitFor();
    const text = await page.locator('#teachingReport').innerText();
    assert(text.includes('头指针为空时') && text.includes('完成标准') && text.includes('可选提升'));
    assert.deepEqual(await page.locator('#teachingReportBody h3').allTextContents(),
      ['总体概括', '最值得注意的发现', '应该如何改', '下次怎么办']);
    for (const dim of ['内容准确', '讲解清楚', '结构连贯', '师生互动']) assert(text.includes(dim));
    assert(!text.includes('提问检查'));
    assert(!text.includes('教师讲授 1 段') && !text.includes('未记录到 AI 学生回答'));
    assert.equal(await page.locator('#teachingReportFacts').count(), 0);
    assert(!text.includes('原文哨兵') && !text.includes('source_ids'));
    assert.equal(await page.locator('#teachingReport details').count(), 0);
    const download = page.waitForEvent('download'); await page.locator('#exportTeachingReport').click();
    const markdown = fs.readFileSync(await (await download).path(), 'utf8');
    assert(markdown.includes('头指针') && markdown.includes('师生互动'));
    assert(!markdown.includes('提问检查') && !markdown.includes('source_ids'));
    fs.mkdirSync(path.join(root, 'logs'), {recursive: true});
    await page.locator('#teachingReport').screenshot({path: path.join(root, 'logs/teaching-report-desktop.png')});
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator('#teachingReport').screenshot({path: path.join(root, 'logs/teaching-report-mobile.png')});
    fail = true; await page.locator('#reloadTeachingReport').click();
    await page.waitForFunction(() => document.getElementById('teachingReportStatus').textContent.includes('模拟读取失败'));
    fail = false; status = 'failed'; await page.locator('#reloadTeachingReport').click();
    await page.getByText('重试教学报告', {exact: true}).waitFor();
    assert(await page.locator('#exportTeachingReport').isDisabled());
    await page.evaluate(() => { const button = document.getElementById('generateTeachingReport'); button.click(); button.click(); });
    await page.getByText('正在核对全课解释、更正与优先改进……', {exact: true}).waitFor();
    assert.equal(posts, 1, 'double retry must submit only once');
    status = 'ready';
    await page.goto(`${base}/?teaching_report=${sid}`);
    await page.getByText('教学报告已生成，可以查看和导出。', {exact: true}).waitFor();
    assert.equal(posts, 1, 'URL restore must not regenerate');
    status = 'idle';
    await page.goto(`${base}/?teaching_report=${sid}`);
    await page.getByText('正在结束课堂并准备报告……', {exact: true}).waitFor();
    status = 'generating';
    await page.getByText('正在核对全课解释、更正与优先改进……', {exact: true}).waitFor();
    status = 'ready';
    await page.getByText('教学报告已生成，可以查看和导出。', {exact: true}).waitFor();
    assert.equal(posts, 1, 'refresh before job registration must poll GET only');
    delayed = {}; delayed.promise = new Promise(resolve => delayed.resolve = resolve);
    const oldRequest = page.waitForRequest(request => request.url().endsWith(sid));
    await page.locator('#reloadTeachingReport').click(); await oldRequest;
    const newSid = 'fedcba543210';
    await page.evaluate(id => window.teachingReport.open(id), newSid);
    await page.getByText('教学报告已生成，可以查看和导出。', {exact: true}).waitFor();
    delayed.resolve(); await new Promise(resolve => setTimeout(resolve, 150));
    assert.equal(await page.locator('#exportTeachingReport').isEnabled(), true);
    const href = await page.evaluate(() => {
      let href;
      const click = HTMLAnchorElement.prototype.click;
      HTMLAnchorElement.prototype.click = function() { href = this.href; };
      document.getElementById('exportTeachingReport').click();
      HTMLAnchorElement.prototype.click = click;
      return href;
    });
    assert(href.endsWith(newSid + '/export'));
    assert.deepEqual(errors, []);
    console.log('PASS: automatic report view, four sections, one retry, polling, download, mobile, failures, read-only restore, stale request isolation');
  } finally {
    if (browser) await browser.close();
    server.kill();
    if (server.exitCode === null) await new Promise(resolve => server.once('exit', resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
