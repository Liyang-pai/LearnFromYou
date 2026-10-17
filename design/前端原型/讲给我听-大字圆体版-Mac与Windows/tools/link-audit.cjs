'use strict';
const fs=require('fs'),path=require('path');
const {pathToFileURL}=require('url');
const {chromium}=require('playwright');
(async()=>{
 const root=path.resolve(__dirname,'..'),url=pathToFileURL(path.join(root,'index.html')).href;
 const browser=await chromium.launch({headless:true,executablePath:process.env.DESIGN_BROWSER_PATH});
 const context=await browser.newContext({offline:true,acceptDownloads:true,viewport:{width:1800,height:1200}});
 const page=await context.newPage(),inventory=[],results=[],errors=[];let baseline,opens=0;
 const cdp=await context.newCDPSession(page);
 await page.addInitScript(()=>{const original=window.setTimeout;window.__auditTimers=[];window.setTimeout=(...args)=>{const id=original(...args);window.__auditTimers.push(id);return id;};});
 page.on('pageerror',e=>errors.push(String(e)));
 const selector='button,a[href],[role="button"],.settings-tabs span,.detail-tabs span,.thumb-actions span,.subtle-link,.blue-text';
 async function ready(id){await page.waitForFunction(id=>document.getElementById('canvas').contentDocument?.body?.dataset.prototypeReady===id,id);}
 async function open(id){if(!baseline||++opens%40===0){await page.goto(url);await ready('01');baseline=await page.evaluate(()=>JSON.stringify(window.prototypeApp.state));await cdp.send('HeapProfiler.collectGarbage');}else{await page.evaluate(b=>{window.__auditTimers.forEach(clearTimeout);window.__auditTimers=[];const state=window.prototypeApp.state;Object.keys(state).forEach(k=>delete state[k]);Object.assign(state,JSON.parse(b));document.getElementById('hostToast').textContent='';document.getElementById('canvas').contentDocument.body.dataset.prototypeReady='';},baseline);}await page.selectOption('#scene',id);await ready(id);}
 try{
  await open('01');const ids=await page.locator('#scene option').evaluateAll(es=>es.map(e=>e.value));
  for(const id of ids){await open(id);inventory.push(...await page.evaluate(({selector,id})=>[...document.getElementById('canvas').contentDocument.querySelectorAll(selector)].map((e,index)=>({screen:id,index,text:(e.textContent||e.getAttribute('aria-label')||'').trim().replace(/\s+/g,' '),tag:e.tagName,href:e.getAttribute('href'),action:e.dataset.action||'',disabled:!!e.disabled,visible:!!e.getClientRects().length})),{selector,id}));}
  fs.writeFileSync(path.join(root,'output','link-inventory.json'),JSON.stringify(inventory,null,2));
  const targets=inventory.filter(e=>e.visible&&!e.disabled&&e.text);
  console.log(JSON.stringify({scenes:ids.length,controls:inventory.length,anchors:inventory.filter(e=>e.tag==='A').length,targets:targets.length}));
  for(let i=0;i<targets.length;i++){
   const target=targets[i];await open(target.screen);
   const before=await page.evaluate(()=>({screen:window.prototypeApp.screen,state:JSON.stringify(window.prototypeApp.state)}));
   let error=null;
   try{await page.evaluate(({selector,index})=>{const el=document.getElementById('canvas').contentDocument.querySelectorAll(selector)[index];el.dispatchEvent(new el.ownerDocument.defaultView.MouseEvent('click',{bubbles:true,cancelable:true}));},{selector,index:target.index});}catch(e){error=String(e);}
   await page.waitForTimeout(35);
   const after=await page.evaluate(()=>({screen:window.prototypeApp.screen,state:JSON.stringify(window.prototypeApp.state),toast:document.getElementById('hostToast').textContent,popup:!!window.prototypeApp.state.popup}));
   const generic=after.toast.startsWith('静态演示：');
   const result={...target,destination:after.screen,stateChanged:before.state!==after.state,popup:after.popup,toast:after.toast,genericPlaceholder:generic,error};results.push(result);
   if(generic||error)console.log(JSON.stringify(result));
   if((i+1)%80===0)console.log('Audited '+(i+1)+' / '+targets.length);
  }
  const result={date:new Date().toISOString(),scope:'逐场景点击可见且启用的按钮/页签/链接；检查即时路由及占位提示，不等同于业务功能验收',sceneCount:ids.length,anchorCount:inventory.filter(e=>e.tag==='A').length,controlCount:targets.length,genericPlaceholders:results.filter(r=>r.genericPlaceholder),errors,results};
  fs.writeFileSync(path.join(root,'output','link-audit.json'),JSON.stringify(result,null,2));
  console.log(JSON.stringify({audited:results.length,genericPlaceholders:result.genericPlaceholders.length,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
