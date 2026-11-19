'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const port = Number(process.env.REPORT_TEST_PORT || 8877);
const base = `http://127.0.0.1:${port}`;
const python = process.env.LFY_TEST_PYTHON || path.join(root, '.venv/Scripts/python.exe');
const sid = '012345abcdef';
const pendingSid = 'badc01d00001';
const archive = process.env.LFY_REPORT_ARCHIVE;
let report;

(async () => {
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'lfy-reflection-browser-'));
  const serverCode = `import asyncio, json, os, runpy, shutil, uvicorn
from pathlib import Path
from backend import app, config
from backend.teaching_report import ReportService, read_classroom, source_hash
fixtures = runpy.run_path('tests/test_teaching_report.py')
original = runpy.run_path('tests/test_report_reflection.py')
root = config.data_root()
path = fixtures['save_classroom'](root)
turns = original['TURNS']
records = [{'type': 'ready', 'data': {'session_id': '${sid}'}, 'elapsed': 0}]
for index, turn in enumerate(turns):
    data = {'type': 'transcript', 'data': turn} if turn['role'] == 'teacher' else {
        'type': 'reply', 'data': {'kind': 'answer', 'text': turn['text']}}
    if index != 2:
        data['elapsed'] = index * 12.25
    records.append(data)
for index in range(4, 22):
    records.append({'type': 'transcript', 'data': {'id': f't{index}', 'mode': 'text', 'text': '继续核对例题的计算步骤。'}})
records.append({'type': 'finished', 'data': {'session_id': '${sid}', 'lesson': {'topic': '广义表'}}, 'elapsed': 100})
path.write_text('\\n'.join(json.dumps(row, ensure_ascii=False) for row in records), encoding='utf-8')
pending = [
    {'type': 'ready', 'data': {'session_id': '${pendingSid}'}},
    {'type': 'audio_input', 'data': {'frames': 1, 'samples': 512}},
    {'type': 'finished', 'data': {'session_id': '${pendingSid}', 'lesson': {'topic': '未转写课例'}}},
]
(path.parent / '${pendingSid}.jsonl').write_text('\\n'.join(json.dumps(row, ensure_ascii=False) for row in pending), encoding='utf-8')
archive = os.getenv('LFY_REPORT_ARCHIVE')
if archive:
    archive = Path(archive)
    for source in (archive, archive.with_name(archive.stem + '.teaching-report-reflection.json')):
        shutil.copyfile(source, path.parent / source.name)
legacy_id = 'fedcba543210'
old = fixtures['save_classroom'].__globals__
old['SID'] = legacy_id
fixtures['save_classroom'](root)
old['SID'] = '${sid}'
cache = root / 'logs' / f'{legacy_id}.teaching-report-v2.json'
cache.write_text(json.dumps({'schema_version': 2, 'status': 'ready', 'report': fixtures['model_output'](),
    'source_hash': source_hash(read_classroom(root, legacy_id), legacy=True)}), encoding='utf-8')
payload = original['example_report']()
t1, t3 = (original['quote'](key) for key in ('t1', 't3'))
t2 = original['quote']('t2', '取最大子表深度再加1。')
payload['assessment'] = {
    'clarity': {'some_steps': [t2], 'main_steps': [t2]},
    'coherence': {'identifiable_topics': [t2], 'ordered_explanation': [t1, t2]},
    'interaction': {'simple_question_or_reply': [t1], 'specific_question': [t1], 'addresses_question': [t2],
                    'understanding_question': [t1]},
    'expression': {'partial_meaning': [t3], 'main_meaning': [t3]},
}
app.reports = ReportService(lambda emit: fixtures['FakeModel'](emit, [], output=payload))
async def seed():
    await app.reports.start('${sid}')
    await app.reports.tasks['${sid}']
asyncio.run(seed())
uvicorn.run(app.app, host='127.0.0.1', port=${port})
`;
  const server = spawn(python, ['-c', serverCode],
    {cwd: root, windowsHide: true, stdio: 'pipe', env: {...process.env, DEEPSEEK_API_KEY: '', Deepseek_API: '',
      LEARNFROMYOU_DATA_ROOT: data, LEARNFROMYOU_SETTINGS_PATH: path.join(data, 'settings.local.json'), PORT: String(port)}});
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
    const seeded = await (await fetch(`${base}/api/teaching-report/${sid}`)).json();
    report = seeded.report;
    assert(report?.findings?.length, 'reflection seed was not generated: ' + (seeded.error || seeded.status));
    browser = await chromium.launch({executablePath: process.env.CHROME_PATH, headless: true});
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    let status = 'generating', posts = 0, fail = false, delayed;
    const response = id => ({...seeded, schema_version: 3, session_id: id, topic: '广义表', status, progress: '正在核对全课解释、更正与优先改进……', facts_text: '教师讲授 1 段；未记录到 AI 学生回答，不能据此判断学生是否理解。',
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
    await page.goto(base + '/debug');
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
    for (const phrase of ['课堂依据', '完成标准', '可选提升', '所以所以所以就是']) assert(text.includes(phrase));
    assert.deepEqual(await page.locator('#teachingReportBody h2').allTextContents(),
      ['本次状态', '改进建议', '表达观察', '下次试试']);
    assert(!text.includes('提问检查'));
    assert(!text.includes('教师讲授 1 段') && !text.includes('未记录到 AI 学生回答'));
    assert.equal(await page.locator('#teachingReportFacts').count(), 0);
    assert(!text.includes('原文哨兵') && !text.includes('source_ids'));
    assert.equal(await page.locator('#teachingReport details').count(), 0);
    const download = page.waitForEvent('download'); await page.locator('#exportTeachingReport').click();
    const markdown = fs.readFileSync(await (await download).path(), 'utf8');
    assert(markdown.includes('通用规则'));
    assert(markdown.includes('所以所以所以就是') && markdown.includes('完成标准'));
    assert(!markdown.includes('提问检查') && !markdown.includes('source_ids'));
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    fail = true; await page.locator('#reloadTeachingReport').click();
    await page.waitForFunction(() => document.getElementById('teachingReportStatus').textContent.includes('模拟读取失败'));
    fail = false; status = 'failed'; await page.locator('#reloadTeachingReport').click();
    await page.getByText('重试教学报告', {exact: true}).waitFor();
    assert(await page.locator('#exportTeachingReport').isDisabled());
    await page.evaluate(() => { const button = document.getElementById('generateTeachingReport'); button.click(); button.click(); });
    await page.getByText('正在核对全课解释、更正与优先改进……', {exact: true}).waitFor();
    assert.equal(posts, 1, 'double retry must submit only once');
    status = 'ready';
    await page.goto(`${base}/debug?teaching_report=${sid}`);
    await page.getByText('教学报告已生成，可以查看和导出。', {exact: true}).waitFor();
    assert.equal(posts, 1, 'URL restore must not regenerate');
    status = 'idle';
    await page.goto(`${base}/debug?teaching_report=${sid}`);
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
    await page.unroute('**/api/teaching-report/**');
    await page.setViewportSize({width: 1440, height: 1000});
    await page.goto(`${base}/report/${sid}`);
    const transcript = page.locator('#transcript-scroll');
    const analysis = page.locator('#analysis-scroll');
    await page.getByRole('heading', {name: '本次教学概览', exact: true}).waitFor();
    assert(await page.getByText('试讲主题：广义表', {exact: false}).isVisible());
    assert(await transcript.isVisible() && await analysis.isVisible());
    const [left, right] = await Promise.all([transcript.boundingBox(), analysis.boundingBox()]);
    assert(left.x + left.width <= right.x, 'desktop must display separate panes side by side');
    assert.equal(await transcript.locator('.report-turn').count(), 22);
    assert.equal(await transcript.locator('[data-turn-id="t1"] time').innerText(), '00:00');
    assert.equal(await transcript.locator('[data-turn-id="r1"] time').innerText(), '00:12');
    assert.equal(await transcript.locator('[data-turn-id="t2"] time').count(), 0);
    assert.equal(await analysis.locator('.report-axis').count(), 5);
    const radar = analysis.locator('.report-radar');
    const points = await radar.locator('.report-radar-dot').evaluateAll(nodes => nodes.map(node =>
      node.getAttribute('cx') + ',' + node.getAttribute('cy')));
    assert.equal(points.length, 4, 'the reference check axis has no numerical score');
    const edges = points.slice(0, -1).map((from, index) => `M${from} L${points[index + 1]}`);
    const sectors = points.slice(0, -1).map((from, index) => `M185,132 L${from} L${points[index + 1]} Z`);
    assert.equal(await radar.locator('.report-radar-partial-line').count(), 1,
      'one pending dimension must still leave connected scored dimensions');
    assert.equal(await radar.locator('.report-radar-partial-line').getAttribute('d'), edges.join(' '));
    assert.equal(await radar.locator('.report-radar-partial-fill').getAttribute('d'), sectors.join(' '));
    assert.equal(await radar.locator('.report-radar-shape').count(), 0);
    assert.match(await analysis.locator('.report-word').filter({hasText: '就是'}).innerText(), /1 次/);
    const clarity = analysis.locator('[data-dimension="clarity"]');
    const tooltip = clarity.locator('.report-axis-tip');
    await clarity.locator('button').first().focus();
    assert(await tooltip.isVisible());
    assert.match(await tooltip.innerText(), /主要步骤/);
    await page.keyboard.press('Escape');
    assert(!(await tooltip.isVisible()));
    await clarity.locator('button').first().hover();
    assert(await tooltip.isVisible());
    await page.mouse.move(0, 0);
    const issue = analysis.locator('details.report-issue').first();
    assert(await issue.evaluate(node => node.open));
    const link = issue.locator('[data-action="report-jump"][data-id="t2"]');
    const quote = await link.getAttribute('data-quote');
    await link.click();
    assert.equal(await transcript.locator('.report-turn.is-linked[data-turn-id="t2"] mark').innerText(), quote);
    const originalPosition = await analysis.evaluate(node => node.scrollTop);
    const transcriptPosition = await transcript.evaluate(node => {node.scrollTop = node.scrollHeight; return node.scrollTop;});
    assert(transcriptPosition > 0, 'desktop transcript scrolls independently');
    assert.equal(await analysis.evaluate(node => node.scrollTop), originalPosition);
    await issue.locator('summary').click();
    assert(!(await issue.evaluate(node => node.open)));
    await issue.locator('summary').click();
    assert(await issue.evaluate(node => node.open));
    assert(await page.getByText('教师能讲清取最大值与数个数的区别。', {exact: false}).isVisible());
    for (const [name, expected] of [['导出 Markdown', '通用规则'], ['导出日志', '"type": "transcript"']]) {
      const download = page.waitForEvent('download');
      await page.getByRole('link', {name, exact: true}).click();
      assert(fs.readFileSync(await (await download).path(), 'utf8').includes(expected));
    }
    if (process.env.LFY_REPORT_SCREENSHOT && !archive) {
      for (const pane of [transcript, analysis]) await pane.evaluate(node => {node.scrollTop = 0;});
      await page.screenshot({path: process.env.LFY_REPORT_SCREENSHOT, fullPage: true});
    }
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    const [upper, lower] = await Promise.all([transcript.boundingBox(), analysis.boundingBox()]);
    assert(upper.y + upper.height <= lower.y, 'narrow report panes must stack');
    await issue.locator('[data-action="report-jump"][data-id="t2"]').click();
    assert.equal(await transcript.locator('.report-turn.is-linked mark').innerText(), quote);
    await page.getByRole('button', {name: '返回教室', exact: true}).click();
    await page.waitForURL(`${base}/history/${sid}`);
    await page.goto(`${base}/report/${pendingSid}`);
    await page.getByRole('heading', {name: '本次教学概览', exact: true}).waitFor();
    assert.equal(await page.locator('.report-issue').count(), 0);
    assert.deepEqual(await page.locator('.report-axis > button > span').allTextContents(), Array(5).fill('资料待核对'));
    assert.equal(await page.locator('.report-radar-shape').count(), 0);
    assert.equal(await page.locator('.report-radar-partial-line, .report-radar-partial-fill, .report-radar-dot').count(), 0);
    await page.getByRole('button', {name: '再创建一次试讲', exact: true}).click();
    await page.waitForURL(`${base}/create`);
    await page.goto(`${base}/report/${newSid}`);
    await page.getByText('师生互动', {exact: true}).waitFor();
    assert.equal(await page.locator('.dimension').count(), 4);
    const reportEndpoint = `${base}/api/teaching-report/${sid}`;
    await page.route(reportEndpoint, route => route.fulfill({json: {...seeded, scores: {...seeded.scores,
      accuracy: {score: 4.5, reason: '模拟参考资料已核对', evidence: []}}}}));
    await page.goto(`${base}/report/${sid}`);
    await radar.locator('.report-radar-shape').waitFor();
    assert.equal(await radar.locator('.report-radar-dot').count(), 5);
    assert.equal(await radar.locator('.report-radar-partial-line, .report-radar-partial-fill').count(), 0);
    await page.unroute(reportEndpoint);
    if (archive) {
      const archivedSid = path.basename(archive, '.jsonl');
      const cachedPath = path.join(path.dirname(archive), archivedSid + '.teaching-report-reflection.json');
      const hasAssessment = Boolean(JSON.parse(fs.readFileSync(cachedPath, 'utf8')).report.assessment);
      await page.goto(`${base}/report/${archivedSid}`);
      await page.getByRole('heading', {name: '本次教学概览', exact: true}).waitFor();
      assert.equal(await transcript.locator('.report-turn').count(), 91);
      assert.equal(await transcript.locator('.report-turn time').count(), 0);
      const axes = await page.locator('.report-axis > button > span').allTextContents();
      assert.equal(axes.length, 5);
      if (hasAssessment) {
        assert(axes.some(value => /^\d\.\d$/.test(value)), 'real assessment must display a grounded score');
        assert.equal(axes[0], '资料待核对', 'accuracy stays pending without trusted reference materials');
      } else {
        assert.deepEqual(axes, Array(5).fill('待评分'));
      }
      const archivedLink = page.locator('.report-issue [data-action="report-jump"]:visible').first();
      const archivedQuote = await archivedLink.getAttribute('data-quote');
      await archivedLink.click();
      assert.equal(await transcript.locator('.report-turn.is-linked mark').innerText(), archivedQuote);
      if (process.env.LFY_REPORT_SCREENSHOT) {
        await page.setViewportSize({width: 1440, height: 1000});
        for (const pane of [transcript, analysis]) await pane.evaluate(node => {node.scrollTop = 0;});
        await page.screenshot({path: process.env.LFY_REPORT_SCREENSHOT, fullPage: true});
      }
    }
    assert.equal(posts, 1);
    assert.deepEqual(errors, []);
    console.log('PASS: formal V3 on desktop and mobile, score reasons, exact evidence, unavailable scores, downloads, navigation, V2, and debug polling/retry' +
      (archive ? ', plus archived 91-turn V3' : ''));
  } finally {
    if (browser) await browser.close();
    server.kill();
    if (server.exitCode === null) await new Promise(resolve => server.once('exit', resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
