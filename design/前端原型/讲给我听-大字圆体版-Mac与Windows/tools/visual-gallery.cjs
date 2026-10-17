'use strict';
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {pathToFileURL}=require('url'),{chromium}=require('playwright');
(async()=>{
 const root=path.resolve(__dirname,'..'),out=path.join(root,'output','visual-refresh'),files=JSON.parse(fs.readFileSync(path.join(out,'verification.json'),'utf8')).checks.map(c=>c.name+'.png').sort();
 assert.equal(files.length,40);
 const escape=s=>s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const style='@font-face{font-family:ChillRound;src:url("../../assets/fonts/ChillRoundF.woff2")}*{box-sizing:border-box}body{margin:0;background:#f2f7ff;color:#344d70;font:17px/1.7 ChillRound,sans-serif}header{max-width:1500px;margin:0 auto;padding:40px 24px 28px}h1{font-size:38px;margin:0 0 7px}p{margin:0;color:#8a9bb1}a{color:inherit;text-decoration:none}.back{float:right;background:#fff;padding:10px 20px;border-radius:16px;color:#5687d0}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px;max-width:1500px;margin:0 auto;padding:0 24px 36px}.page{background:white;border-radius:22px;overflow:hidden;border:5px solid white;box-shadow:0 6px 25px #44678c05}.page img{display:block;width:100%;aspect-ratio:1.6;object-fit:cover}.label{padding:12px 16px;font-size:18px}.page:hover{box-shadow:0 9px 30px #44678c12}';
 const cards=names=>names.map(name=>'<a class="page" href="'+encodeURI('png/'+name)+'"><img src="'+encodeURI('png/'+name)+'" alt="'+escape(name)+'"><div class="label">'+escape(name.slice(0,-4).replaceAll('-',' / '))+'</div></a>').join('');
 const html=(names,overview=false)=>'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>讲给我听 · 大字圆体版</title><style>'+style+'</style><header>'+(overview?'':'<a class="back" href="../../index.html">打开交互原型 →</a>')+'<h1>字大一点，陪伴也近一点。</h1><p>讲给我听 · 大字圆体版 / '+(overview?'六页预览':'40个页面与状态，点击图片查看PNG原图')+'</p>'+(overview?'':'<p style="font-size:14px;margin-top:8px">以下为页面首屏快照；完整内容可在交互原型中滚动查看。</p>')+'</header><main class="grid">'+cards(names)+'</main></html>';
 fs.writeFileSync(path.join(out,'index.html'),html(files));
 const selected=['01','03','08','20','22','24'].map(id=>files.find(n=>n.startsWith(id+'-')));fs.writeFileSync(path.join(out,'overview.html'),html(selected,true));
 const b=await chromium.launch({headless:true,executablePath:process.env.DESIGN_BROWSER_PATH});try{
 const page=await b.newPage({viewport:{width:1560,height:1800},offline:true});await page.goto(pathToFileURL(path.join(out,'overview.html')).href);await page.evaluate(()=>document.fonts.ready);assert(await page.evaluate(()=>[...document.images].every(i=>i.complete&&i.naturalWidth)));await page.screenshot({path:path.join(out,'六页总览.png'),fullPage:true});
 await page.goto(pathToFileURL(path.join(out,'index.html')).href);assert.equal(await page.locator('.page').count(),40);assert(await page.evaluate(()=>[...document.images].every(i=>i.complete&&i.naturalWidth)));console.log('40张PNG图册与六页总览已生成、图片载入通过');
 }finally{await b.close();}
})().catch(e=>{console.error(e);process.exit(1);});
