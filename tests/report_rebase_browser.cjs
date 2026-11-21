// 只读检查真实验收报告的路由、刷新、五维详情、统计和导出，并保存桌面及移动截图。
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
const {chromium}=require('playwright');
const base=process.env.LFY_BROWSER_BASE||'http://127.0.0.1:8766';
const output=process.env.LFY_BROWSER_OUTPUT||path.resolve('logs/report-rebase-20261121/screenshots');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],writes=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',r=>{if(r.method()==='POST')writes.push(r.url());});
  await fs.mkdir(output,{recursive:true});
  for(const route of ['/','/debug']){
   await page.goto(base+route);await page.reload();
   assert.equal(await page.locator('body').count(),1);
  }
  for(const [name,sid] of [['earth','73602980bfb3'],['short','abc123000001'],['long','abc123000002']]){
   const report=await (await page.request.get(base+'/api/teaching-report/'+sid)).json();
   assert.equal(report.status,'ready');assert.equal(Object.keys(report.scores).length,5);
   await page.goto(base+'/report/'+sid);await page.getByRole('heading',{name:'本次教学概览',exact:true}).waitFor();
   await page.reload();await page.getByRole('heading',{name:'本次教学概览',exact:true}).waitFor();
   assert.equal(await page.locator('.report-axis').count(),5);
   assert.equal(await page.locator('[data-transcription-notice]').count(),1);
   assert.equal(await page.locator('.report-word').count(),(report.word_counts||[]).length);
   await page.screenshot({path:path.join(output,name+'-desktop.png'),fullPage:true});
   await page.locator('[data-action="report-accuracy"]').click();
   await page.locator('#report-accuracy-details').waitFor({state:'visible'});
   assert.ok((await page.locator('#report-accuracy-details').innerText()).includes('学科依据'));
   await page.screenshot({path:path.join(output,name+'-accuracy.png'),fullPage:true});
   const evidence=page.locator('#report-accuracy-details [data-action="report-jump"]').first();
   const quote=await evidence.getAttribute('data-quote');await evidence.click();
   assert.equal(await page.locator('.report-turn.is-linked mark').innerText(),quote);
   const markdown=await page.request.get(base+'/api/teaching-report/'+sid+'/export');
   assert.equal(markdown.status(),200);assert.equal((await markdown.text()).split(report.transcription_notice).length-1,1);
   await page.setViewportSize({width:390,height:844});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.screenshot({path:path.join(output,name+'-mobile.png'),fullPage:true});
   await page.setViewportSize({width:1440,height:1000});
  }
  assert.deepEqual(writes,[]);assert.deepEqual(errors,[]);
  console.log('PASS: actual earth/short/long reports, canonical text, five axes, accuracy evidence, fillers, export, routes/refresh, zero POST and page errors');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
