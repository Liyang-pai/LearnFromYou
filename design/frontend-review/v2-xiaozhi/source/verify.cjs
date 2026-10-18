// 只检查静态设计产物：解码 PNG、检查边界与正式界面排除项，不连接业务服务。
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const sharp=require('sharp');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
(async()=>{
 const manifest=JSON.parse(fs.readFileSync(path.join(root,'manifest.json'),'utf8'));
 const browser=await chromium.launch({...(process.env.DESIGN_BROWSER_PATH?{executablePath:process.env.DESIGN_BROWSER_PATH}:{}),headless:true,args:['--disable-gpu','--disable-background-networking']});
 const context=await browser.newContext({viewport:{width:1600,height:1000},offline:true});
 const page=await context.newPage();const failures=[],results=[],pageErrors=[];
 const asset=fs.readFileSync(path.join(root,manifest.character.file));
 const assetHash=crypto.createHash('sha256').update(asset).digest('hex');
 // 用户原始文件已在本地交付时核对；共享版按清单中记录的原图摘要验证。
 const originalHash=manifest.character.sha256;
 if(assetHash!==originalHash||assetHash!==manifest.character.sha256)failures.push('角色原图字节校验失败');
 page.on('pageerror',e=>pageErrors.push(String(e)));
 for(const item of manifest.pages){
  const {info}=await sharp(path.join(root,item.png)).raw().toBuffer({resolveWithObject:true});
  if(info.width!==1920||info.height!==1200)failures.push(item.name+':尺寸错误');
  await page.goto('file:///'+path.join(root,'html',item.name+'.html').replace(/\\/g,'/'));
  await page.evaluate(()=>Promise.all([...document.images].map(i=>i.decode())));
  const check=await page.evaluate(()=>{
   const text=document.querySelector('.main').innerText;
   return {internalLinks:/\b[tpqk]\d+\b/.test(text),recordingUpload:/上传录音|上传音频|选择音频文件|WAV|MP3|M4A/.test(text),oldNav:[...document.querySelectorAll('.nav-item')].some(e=>e.innerText.trim()==='学生状态'),modalOutOfBounds:[...document.querySelectorAll('.modal')].some(e=>{const b=e.getBoundingClientRect();return b.top<0||b.bottom>1001||b.left<0||b.right>1601}),missingMascotSlot:!document.querySelector('.mascot')&&document.querySelector('.stage-panel')!==null,characterImages:document.images.length,characterLoaded:[...document.images].every(i=>i.complete&&i.naturalWidth===700&&i.naturalHeight===700),oldPlaceholder:/角色素材预留位置|小纸互动区/.test(document.body.innerText),characterDistorted:[...document.images].some(i=>{const b=i.getBoundingClientRect();return Math.abs(b.width-b.height)>1;}),stageCharacterCropped:[...document.querySelectorAll('.stage-panel .mascot img')].some(i=>{const b=i.getBoundingClientRect(),s=i.closest('.stage-panel').getBoundingClientRect();return b.top<s.top||b.bottom>s.bottom||b.left<s.left||b.right>s.right})};
  });
  const debug=item.category==='保留调试';
  if(!debug&&(check.internalLinks||check.recordingUpload))failures.push(item.name+':正式界面包含排除项');
  if(check.oldNav||check.modalOutOfBounds||check.missingMascotSlot)failures.push(item.name+':页面结构检查未通过');
  if(!check.characterLoaded||check.characterImages<1||check.oldPlaceholder||check.characterDistorted||check.stageCharacterCropped)failures.push(item.name+':角色替换检查未通过');
  if(item.qa.overflow.length)failures.push(item.name+':出图阶段存在裁切');
  results.push({name:item.name,decoded:true,...check});
 }
 await page.goto('file:///'+path.join(root,'index.html').replace(/\\/g,'/'));
 const gallery=await page.evaluate(()=>({count:document.images.length,loaded:[...document.images].every(i=>i.complete&&i.naturalWidth===1920)}));
 if(gallery.count!==42||!gallery.loaded)failures.push('图册图片加载检查失败');
 const report={verified:'2026-10-17 Asia/Shanghai',type:'静态设计产物验证；不是产品功能测试',version:'小纸原图替换版',characterOriginalBytesPreserved:assetHash===originalHash,allCharacterImagesLoaded:results.every(r=>r.characterLoaded),oldPlaceholdersRemoved:results.every(r=>!r.oldPlaceholder),pngCount:manifest.pages.length,dimensions:'1920 × 1200',pngFullyDecoded:results.every(r=>r.decoded),viewportOverflowPages:manifest.pages.filter(p=>p.qa.overflow.length).map(p=>p.name),gallery,pageErrors,failures,results};
 fs.writeFileSync(path.join(root,'verification.json'),JSON.stringify(report,null,2),'utf8');await browser.close();
 console.log(JSON.stringify({pngCount:report.pngCount,decoded:report.pngFullyDecoded,characterOriginalBytesPreserved:report.characterOriginalBytesPreserved,allCharacterImagesLoaded:report.allCharacterImagesLoaded,oldPlaceholdersRemoved:report.oldPlaceholdersRemoved,gallery,failures,pageErrors},null,2));
 if(failures.length||pageErrors.length)process.exit(1);
})().catch(e=>{console.error(e);process.exit(1)});
