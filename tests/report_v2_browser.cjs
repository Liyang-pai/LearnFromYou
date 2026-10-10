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
    let fixture = JSON.parse(fs.readFileSync(path.join(root,'docs/report-v2-examples/正确讲解.json'),'utf8'));
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
    assert.equal(await page.locator('#reportDetails').evaluate(el=>el.open),false);
    assert((await page.locator('#reportAnalysis h3').first().innerText()).includes('本节讲授要点'));
    assert.equal(await page.locator('#reportSummary').isVisible(),false,'stats should be folded by default');
    assert.equal(await page.locator('#reportSummary .report-metric').count(),Object.keys(fixture.snapshot.settlement.metrics).length);
    assert((await page.locator('#reportAnalysis').innerText()).includes('AI 模拟'));
    await page.locator('#reportDetails > summary').click();
    const verificationSection=page.locator('.report-section').filter({has:page.getByRole('heading',{name:'学生真的理解了吗',exact:true})});
    assert.equal(await verificationSection.locator('.report-citations button').count(),1,'same-event references must share one button');
    await page.locator('details:has(> .report-citations)').first().locator('summary').click();
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
    assert.equal(await page.locator('#reportSummary .report-metric').count(),Object.keys(fixture.snapshot.settlement.metrics).length);
    fail=false; await page.locator('#reportReload').click(); await page.getByText('报告已生成。',{exact:true}).waitFor();
    // Optional read-only replay of the actual acceptance cache, with no model or classroom calls.
    if (process.env.REPORT_ACCEPTANCE_VIEW) {
      fixture=JSON.parse(fs.readFileSync(process.env.REPORT_ACCEPTANCE_VIEW,'utf8'));
      await page.evaluate(data=>window.reportV2.finished(data),{session_id:fixture.snapshot.session_id,settlement:fixture.snapshot.settlement});
      await page.getByText('报告已生成。',{exact:true}).waitFor();
      assert.equal(await page.locator('#reportDetails').evaluate(el=>el.open),false);
      await page.locator('#reportV2').evaluate(el=>el.scrollIntoView());
      await page.screenshot({path:path.join(root,'logs/report-quality-first-view.png')});
      await page.locator('#reportDetails > summary').click();
      const summary=await page.locator('#reportSummary').innerText();
      assert(summary.includes('未回应为零不表示全部解决'));
      if (fixture.snapshot.settlement.questions.some(q=>q.status==='deferred')) assert(summary.includes('暂缓处理，尚未解决'));
      for (const metric of Object.values(fixture.snapshot.settlement.metrics)) {
        const readerMetric=Object.values(fixture.reader.metrics).find(item=>item.method===metric.method);
        assert((fixture.record_markdown || fixture.markdown).includes(`${readerMetric.label}：${metric.value===null?'无法统计':metric.value}`));
        assert(summary.includes(readerMetric.label));
      }
      await page.locator('details:has(> .report-citations)').evaluateAll(rows=>rows.forEach(row=>row.open=true));
      const buttons=page.locator('.report-citations button');
      assert(await buttons.count()>=5);
      for (let i=0;i<await buttons.count();i++) {
        const button=buttons.nth(i), id=await button.getAttribute('data-event-id');
        await button.click();
        const row=page.locator('.report-highlight');
        assert((await row.getAttribute('id')).endsWith(id.replace(/[^a-zA-Z0-9_-]/g,'-')));
        const marked=await row.locator('mark').allTextContents();
        assert(marked.length && marked.every(text=>fixture.snapshot.evidence[id].text.includes(text)));
      }
      assert(!(await page.locator('#reportAnalysis').innerText()).includes('tentative'));
      assert(!(await page.locator('#reportAnalysis').innerText()).includes('有理解证据'));
      // Report payload and teacher-facing Markdown use one reader projection.
      assert((await page.locator('#reportAnalysis').innerText()).includes(fixture.reader.summary));
      assert(fixture.markdown.includes(fixture.reader.summary));
      for (const issue of fixture.reader.issues) {
        assert((await page.locator('#reportAnalysis').innerText()).includes(issue.suggestion.action));
        assert(fixture.markdown.includes(issue.suggestion.action));
      }
      await page.screenshot({path:path.join(root,'logs/report-v2-fixed-mobile.png'),fullPage:true});
      await page.setViewportSize({width:1440,height:1000});
      await page.screenshot({path:path.join(root,'logs/report-v2-fixed-desktop.png'),fullPage:true});
      await page.locator('#reportEvidence details').evaluateAll(rows=>rows.forEach(row=>row.open=false));
      await page.locator('#reportDetails').evaluate(el=>el.open=false);
      await page.locator('#reportV2').evaluate(el=>el.scrollIntoView());
      await page.screenshot({path:path.join(root,'logs/report-v2-fixed-summary.png')});
      await page.locator('#reportAnalysis').evaluate(el=>el.scrollIntoView());
      await page.screenshot({path:path.join(root,'logs/report-v2-fixed-analysis.png')});
      console.log('PASS actual acceptance replay: all citation buttons/highlights, question states, conservative legacy verdicts, frontend/Markdown metrics');
    }
    // Explicit generation failure must be prominent and expose retry, never imply no problem.
    const originalFixture=fixture;
    fixture={...fixture,status:'partial',diagnosis:null,reader:{...fixture.reader,summary:'教学分析生成失败，这不表示没有教学问题。',diagnosis_failed:true},
      stages:{...fixture.stages,diagnosis:{status:'failed',error:'报告两次结构校验失败'}}};
    await page.locator('#reportReload').click();
    await page.locator('#reportAnalysis').getByText('教学分析生成失败，这不表示没有教学问题。',{exact:true}).waitFor();
    assert.equal(await page.locator('#reportAnalysis').getByRole('button',{name:'重试未完成分析'}).count(),1);
    assert(!(await page.locator('#reportAnalysis').innerText()).includes('暂无有依据的建议'));
    fixture=originalFixture;
    // Opening a persisted report from its URL must use GET only and retain saved feedback.
    const reportMethods=[];
    page.on('request',req=>{ if(new URL(req.url()).pathname.endsWith('/report-v2')) reportMethods.push(req.method()); });
    await page.goto(base+'/?report='+fixture.snapshot.session_id);
    await page.getByText('报告已生成。',{exact:true}).waitFor();
    assert.deepEqual(reportMethods,['GET']);
    assert.equal(await page.locator('#reportDetails').evaluate(el=>el.open),false);
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
