// 使用真实无头浏览器和模拟报告，验证结算、证据定位、导出、故障与跨课堂隔离。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');

async function main() {
  const root = path.resolve(__dirname, '..'), port = Number(process.env.REPORT_TEST_PORT || 8876);
  const base = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.LFY_TEST_PYTHON || 'python', ['-B','-m','uvicorn','backend.app:app','--host','127.0.0.1','--port',String(port)], {
    cwd:root, windowsHide:true, env:{...process.env, PORT:String(port), DEEPSEEK_API_KEY:'', Deepseek_API:''}, stdio:['ignore','pipe','pipe']
  });
  let serverOutput = '';
  server.stdout.on('data', data => { serverOutput += data; }); server.stderr.on('data', data => { serverOutput += data; });
  let browser;
  try {
    for (let i=0; i<100; i++) {
      if (server.exitCode !== null) throw new Error('测试服务启动失败：'+serverOutput);
      try { if ((await fetch(base+'/api/health')).ok) break; } catch {}
      await new Promise(resolve => setTimeout(resolve,100));
    }
    browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
    const page = await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
    page.on('pageerror', error => errors.push(error.message));
    const fixture = JSON.parse(fs.readFileSync(path.join(root,'docs/report-v2-examples/正确讲解.json'),'utf8'));
    fixture.markdown = fs.readFileSync(path.join(root,'docs/report-v2-examples/正确讲解.md'),'utf8');
    let fail=false, delayed=null, release=null;
    await page.route('**/api/sessions/*/report-v2',async route => {
      if (delayed) { release(); await delayed; }
      if (fail) return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'模拟服务暂时不可用'})});
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(fixture)});
    });
    await page.goto(base); await page.waitForFunction(() => Boolean(window.reportV2));
    await page.evaluate(data => window.reportV2.finished(data),{session_id:fixture.snapshot.session_id,settlement:fixture.snapshot.settlement});
    await page.getByText('报告已生成。',{exact:true}).waitFor();
    assert.equal(await page.locator('#reportSummary .report-metric').count(),9);
    assert((await page.locator('#reportAnalysis').innerText()).includes('模拟验证'));
    await page.locator('.report-citations button').first().click();
    const highlighted=page.locator('.report-highlight');
    assert((await highlighted.getAttribute('id')).endsWith('e000002'));
    assert(await highlighted.evaluate(el => el.open)); assert(await highlighted.locator('mark').count());
    const downloadEvent = page.waitForEvent('download'); await page.locator('#reportDownload').click();
    const download = await downloadEvent; assert(download.suggestedFilename().endsWith('.md'));
    assert(fs.readFileSync(await download.path(),'utf8').includes('模拟数据'));
    await page.screenshot({path:path.join(root,'docs/report-v2-examples/桌面预览.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path:path.join(root,'docs/report-v2-examples/手机预览.png'),fullPage:true});
    fail=true; await page.locator('#reportReload').click();
    await page.waitForFunction(() => document.getElementById('reportStatus').textContent.includes('模拟服务暂时不可用'));
    assert.equal(await page.locator('#reportSummary .report-metric').count(),9);
    fail=false; await page.locator('#reportReload').click(); await page.getByText('报告已生成。',{exact:true}).waitFor();
    // Hold an old response, reset for a new classroom, then release it.
    let unblock, waiting;
    delayed=new Promise(resolve => { unblock=resolve; }); waiting=new Promise(resolve => { release=resolve; });
    await page.locator('#reportReload').click(); await waiting;
    await page.evaluate(() => window.reportV2.reset()); unblock(); delayed=null;
    await page.waitForTimeout(200);
    assert(await page.locator('#reportV2').evaluate(el => el.classList.contains('hidden')));
    assert.equal(await page.locator('#reportSummary').innerText(),'');
    assert.deepEqual(errors,[]);
    console.log('PASS browser: report sections, correct evidence highlight, UTF-8 Markdown download, desktop/mobile layout, failure retention/reload, stale-response isolation, no page errors');
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) { server.kill(); await new Promise(resolve => server.once('exit',resolve)); }
  }
}
main().catch(error => { console.error(error); process.exitCode=1; });
