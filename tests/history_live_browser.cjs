'use strict';
// Uses an already-running bounded history_server.py --live and history_live.py result.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const data=process.env.HISTORY_TEST_DATA, base=`http://127.0.0.1:${process.env.HISTORY_TEST_PORT||8895}`;
if(!data) throw Error('HISTORY_TEST_DATA is required');
(async()=>{
  const evidence=JSON.parse(fs.readFileSync(path.join(data,'live-classroom.json')));
  const usage=()=>JSON.parse(fs.readFileSync(path.join(data,'usage.json')));
  const browser=await chromium.launch({executablePath:process.env.CHROME_PATH,headless:true});
  const errors=[],failures=[];
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror',e=>errors.push(e.message));
    page.on('response',r=>{if(r.status()>=400)failures.push(r.url()+':'+r.status());});
    await page.goto(base+'/history/'+evidence.copy_session_id);
    await page.getByText('验收注入的一次报告失败，点击重试继续',{exact:true}).waitFor({timeout:30000});
    assert.equal(await page.locator('.history-turn').count(),evidence.dialogue_count);
    assert((await page.locator('#historyDialogue').innerText()).includes(evidence.student_reply));
    const failedRequests=usage().requests.length;
    await page.reload();await page.locator('#retryHistoryReport').waitFor();assert.equal(usage().requests.length,failedRequests);
    await page.locator('#retryHistoryReport').click();
    // Generation belongs to the backend: leave and reopen while it is running.
    await page.locator('#backHistory').click();await page.goto(base+'/history/'+evidence.copy_session_id);
    await page.getByText('教学报告已生成。',{exact:true}).waitFor({timeout:310000});
    const completeRequests=usage().requests.length;
    await page.reload();await page.getByText('教学报告已生成。',{exact:true}).waitFor();assert.equal(usage().requests.length,completeRequests);
    assert((await page.locator('#historyReportBody').innerText()).includes('惯性'));
    const download=page.waitForEvent('download');await page.locator('#downloadHistory').click();
    assert.equal(fs.readFileSync(await(await download).path(),'utf8'),fs.readFileSync(path.join(data,`logs/${evidence.copy_session_id}.jsonl`),'utf8'));
    await page.screenshot({path:path.join(data,'live-history-detail.png'),fullPage:true});
    await page.locator('#backHistory').click();await page.locator('.history-card').first().waitFor();
    await page.screenshot({path:path.join(data,'live-history-list.png'),fullPage:true});
    // Make a second missing-report copy: opening two pages must create one real job.
    const parallelSid='123456abcddd';
    fs.writeFileSync(path.join(data,`logs/${parallelSid}.jsonl`),fs.readFileSync(path.join(data,`logs/${evidence.session_id}.jsonl`),'utf8').replaceAll(evidence.session_id,parallelSid));
    const second=await browser.newPage();
    await Promise.all([page.goto(base+'/history/'+parallelSid),second.goto(base+'/history/'+parallelSid)]);
    await Promise.all([page.getByText('教学报告已生成。',{exact:true}).waitFor({timeout:310000}),second.getByText('教学报告已生成。',{exact:true}).waitFor({timeout:310000})]);
    assert.equal(usage().requests.length,completeRequests+1,'two history pages should share one report request');
    await second.close();
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(data,'live-history-mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);assert.deepEqual(failures,[]);
    const result={result:'PASS',requests:usage().requests.length,injectedFailures:usage().injected_failures,
      checks:['real student reply','historical original profile','complete dialogue','missing report auto-start','injected failure no paid call','manual retry real report','navigation during generation','cached reload no calls','parallel opens single real job','original log download','desktop/mobile','no browser errors']};
    fs.writeFileSync(path.join(data,'browser-validation.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
