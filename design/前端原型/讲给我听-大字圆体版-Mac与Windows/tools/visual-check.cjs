'use strict';
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {pathToFileURL}=require('url'),{chromium}=require('playwright');
(async()=>{
 const root=path.resolve(__dirname,'..'),out=path.join(root,'output','visual-refresh');fs.mkdirSync(path.join(out,'png'),{recursive:true});
 const browser=await chromium.launch({headless:true,executablePath:process.env.DESIGN_BROWSER_PATH});
 try{
 const context=await browser.newContext({viewport:{width:1800,height:1200},offline:true}),page=await context.newPage(),errors=[],consoleErrors=[],external=[],checks=[];
 page.on('pageerror',e=>errors.push(String(e)));page.on('console',m=>{if(m.type()==='error')consoleErrors.push(m.text());});page.on('request',r=>{if(/^(https?|wss?):/.test(r.url()))external.push(r.url());});
 const wait=async id=>page.waitForFunction(id=>document.getElementById('canvas').contentDocument?.body?.dataset.prototypeReady===id,id);
 await page.goto(pathToFileURL(path.join(root,'index.html')).href);await wait('01');
 const ids=await page.locator('#scene option').evaluateAll(es=>es.map(e=>e.value));
 for(const id of ids){
   await page.selectOption('#scene',id);await wait(id);await page.evaluate(async()=>{await document.fonts.ready;await document.getElementById('canvas').contentDocument.fonts.ready;});
   const result=await page.evaluate(()=>{const d=document.getElementById('canvas').contentDocument,m=d.querySelector('.main');return {id:window.prototypeApp.screen,name:window.PROTOTYPE_SCREENS[window.prototypeApp.screen].name,font:[...d.fonts].map(f=>({family:f.family,status:f.status})),bodyFont:parseFloat(d.defaultView.getComputedStyle(d.body).fontSize),images:[...d.images].every(i=>i.complete&&i.naturalWidth),mainWidth:m.clientWidth,mainScrollWidth:m.scrollWidth,bodyWidth:d.body.clientWidth,bodyScrollWidth:d.body.scrollWidth,mainHeight:m.clientHeight,mainScrollHeight:m.scrollHeight,hostOverflow:document.documentElement.scrollWidth>innerWidth};});
   assert(result.font.some(f=>f.family==='ChillRound'&&f.status==='loaded'),'圆体字体未载入 '+id);
   assert(result.images,'图片未载入 '+id);assert(result.bodyFont>=18,'字体未加大 '+id);assert(!result.hostOverflow,'宿主页横向溢出 '+id);
   result.horizontalOverflow=result.mainScrollWidth>result.mainWidth+1||result.bodyScrollWidth>result.bodyWidth+1;
   result.primaryActions=await page.evaluate(()=>{const d=document.getElementById('canvas').contentDocument,selector=d.querySelector('.backdrop')?'.modal-footer .btn':'.profile-card .footer .btn,.profile-form>.btn.full:last-of-type,.savebar button';return [...d.querySelectorAll(selector)].filter(e=>!e.disabled).map(e=>{const r=e.getBoundingClientRect(),hit=d.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return {label:e.textContent.trim(),visible:r.top>=0&&r.bottom<=d.defaultView.innerHeight,unobstructed:hit&&(hit===e||e.contains(hit))};});});
   for(const action of result.primaryActions){assert(action.visible&&action.unobstructed,'主要操作不可见或被遮挡 '+id+' '+action.label);}
   checks.push(result);
   await page.frameLocator('#canvas').locator('body').screenshot({path:path.join(out,'png',result.name+'.png')});
 }
 await page.setViewportSize({width:1440,height:900});await page.selectOption('#scene','08');await wait('08');await page.evaluate(async()=>{await document.getElementById('canvas').contentDocument.fonts.ready;});await page.screenshot({path:path.join(out,'笔记本-我的教室.png')});
 const result={date:new Date().toISOString(),sceneCount:checks.length,errors,consoleErrors,externalRequests:external,horizontalOverflows:checks.filter(c=>c.horizontalOverflow).map(c=>c.id),checks};
 fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify(result,null,2));console.log(JSON.stringify({scenes:checks.length,errors,consoleErrors,externalRequests:external,horizontalOverflows:result.horizontalOverflows}));
 assert.equal(errors.length,0);assert.equal(consoleErrors.length,0);assert.equal(external.length,0);assert.equal(result.horizontalOverflows.length,0);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
