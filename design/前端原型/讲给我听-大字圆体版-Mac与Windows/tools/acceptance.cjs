// 用真实浏览器验证本地原型完整交互、下载、42个场景与网络隔离；不运行产品服务。
'use strict';
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {pathToFileURL}=require('url');
const {chromium}=require('playwright');
(async()=>{
 const root=path.resolve(__dirname,'..'),out=path.join(root,'output');fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({headless:true,...(process.env.DESIGN_BROWSER_PATH?{executablePath:process.env.DESIGN_BROWSER_PATH}:{})});
 try{
 const context=await browser.newContext({viewport:{width:1600,height:1100},offline:true,acceptDownloads:true});
 const page=await context.newPage(),errors=[],external=[],checks=[];let microphoneRequests=0;
 page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(/^(https?|wss?):/.test(r.url()))external.push(r.url());});
 await page.addInitScript(()=>{navigator.mediaDevices.getUserMedia=()=>{window.__micRequests=(window.__micRequests||0)+1;throw Error('原型不应申请麦克风');};});
 const wait=async id=>{try{await page.waitForFunction(s=>document.getElementById('canvas').contentDocument?.body?.dataset.prototypeReady===s,id,{timeout:10000});}catch(e){console.log('WAIT FAILED',id,await page.evaluate(()=>({screen:window.prototypeApp.screen,state:window.prototypeApp.state,ready:document.getElementById('canvas').contentDocument?.body?.dataset.prototypeReady})));await page.screenshot({path:path.join(out,'failure.png')});throw e;}};
 const canvas=()=>page.frameLocator('#canvas');
 const choose=async id=>{await page.selectOption('#scene',id);await wait(id);};
 const button=(name)=>canvas().getByRole('button',{name,exact:true});
 await page.goto(pathToFileURL(path.join(root,'index.html')).href);await wait('01');
 await button('创建一次试讲').click();await wait('02');
 await canvas().getByLabel('教学主题',{exact:true}).fill('惯性的概念');
 await canvas().getByLabel('年级',{exact:true}).selectOption('自定义…');await canvas().getByLabel('自定义内容').fill('大学 · 大二');await button('保存').click();await wait('02');assert.equal(await canvas().getByLabel('年级',{exact:true}).inputValue(),'大学 · 大二');await canvas().getByLabel('年级',{exact:true}).selectOption('初中 · 八年级');checks.push('自定义年级输入与表单回填');
 await button('生成学生画像').click();await wait('03');
 await button('确认设定，创建试讲').click();await wait('05');
 await canvas().getByRole('button',{name:'麦克风（模拟）'}).click();await wait('06');
 await canvas().getByLabel('讲解或提问').fill('公交车刹车时，人为什么会向前倾？');await button('发送').click();await wait('08');
 assert.equal(await page.evaluate(()=>window.prototypeApp.state.session.messages.length),2);checks.push('填写主题→模拟画像→创建→模拟麦克风→文字问答');
 await canvas().getByText('文字显示',{exact:true}).click();await canvas().locator('.speech-bubble').waitFor({state:'hidden'});assert.equal(await canvas().locator('.chat-text').count(),2);await canvas().getByText('文字显示',{exact:true}).click();await canvas().locator('.speech-bubble').waitFor({state:'visible'});checks.push('中央字幕独立开关，不删除对话');
 const imageChoice=page.waitForEvent('filechooser');await button('添加或更换截图').click();await(await imageChoice).setFiles(path.join(root,'assets','小纸-用户原图.png'));await canvas().locator('.chip-name').filter({hasText:'小纸-用户原图.png'}).waitFor();await wait('08');checks.push('对话框加号选图后直接显示缩略图，无独立确认页');
 await button('放大预览教学截图').click();await canvas().getByRole('dialog',{name:'教学截图放大预览'}).waitFor();await button('关闭图片预览').click();await wait('08');assert.equal(await page.evaluate(()=>window.prototypeApp.state.image.name),'小纸-用户原图.png');checks.push('图片浮层预览关闭后保留当前截图与课堂');
 await page.click('#fault');await button('截图上传失败').click();await canvas().getByRole('alert').waitFor();assert.equal(await page.evaluate(()=>window.prototypeApp.state.image.name),'小纸-用户原图.png');const retryImage=page.waitForEvent('filechooser');await button('重新选择').click();await(await retryImage).setFiles(path.join(root,'assets','小纸-用户原图.png'));await canvas().getByRole('alert').waitFor({state:'hidden'});checks.push('模拟截图失败行内提示，旧图保留并可重新选择');
 assert.equal(await button('课堂偏好').count(),0);assert.equal(await page.locator('#scene option[value="17"]').count(),0);
 await canvas().locator('.nav-item').filter({hasText:'设置'}).first().click();await wait('22');await canvas().getByRole('switch',{name:'允许学生发言',exact:true}).click();await wait('22');await button('保存设置').click();await wait('22');await canvas().locator('.nav-item').filter({hasText:'我的教室'}).click();await wait('09');
 await canvas().getByLabel('讲解或提问').fill('关闭学生发言后继续讲授。');await button('发送').click();await wait('09');assert.equal(await page.evaluate(()=>window.prototypeApp.state.session.messages.filter(m=>m.role==='student').length),1);checks.push('禁止学生发言时仍接收文字，保留原对话');
 await button('生成报告').click();await wait('18');await button('结束并生成报告').click();await wait('20');
 const reportDownload=page.waitForEvent('download');await button('导出 Markdown').click();const report=await reportDownload;const reportText=fs.readFileSync(await report.path(),'utf8');assert(reportText.includes('静态模拟报告'));checks.push('结束确认→模拟报告→真实本地Markdown下载');
 await canvas().locator('.nav-item').filter({hasText:'设置'}).first().click();await wait('22');
 await canvas().getByLabel('接口地址',{exact:true}).fill('https://api.example.com/v1');await canvas().locator('.nav-item').filter({hasText:'首页'}).click();await wait('37');await button('继续编辑').click();await wait('23');assert.equal(await canvas().getByLabel('接口地址',{exact:true}).inputValue(),'https://api.example.com/v1');await button('保存设置').click();await wait('22');
 await button('测试文字连接').click();await wait('22');await canvas().getByText('模拟通过',{exact:true}).waitFor();checks.push('模拟设置编辑、未保存保护、保存与连接演示');
 await canvas().locator('.settings-tabs span').filter({hasText:'语音与识别'}).click();await wait('24');
 await canvas().locator('.model-card').filter({hasText:'Whisper Small'}).getByRole('button',{name:'下载模型',exact:true}).click();await wait('25');await wait('24');assert(await page.evaluate(()=>window.prototypeApp.state.installed.includes('Whisper Small')));checks.push('本地定时器模拟模型下载进度与完成');
 await button('进入').click();await wait('26');await button('开始录音测试').click();await page.waitForFunction(()=>window.prototypeApp.state.asr.records.length===3);await button('结束录音').click();await wait('26');
 const asrDownload=page.waitForEvent('download');await button('导出结果').click();const asr=await asrDownload;assert(JSON.parse(fs.readFileSync(await asr.path(),'utf8')).records.length===3);await button('清空结果').click();await wait('26');assert.equal(await page.evaluate(()=>window.prototypeApp.state.asr.records.length),0);checks.push('固定样例模拟ASR、停止、清空与本地导出');
 await page.click('#fault');await button('画像生成失败').click();await wait('32');checks.push('模拟画像失败与草稿保留');
 await page.click('#fault');await button('报告生成失败').click();await wait('21');await button('重试生成报告').click();await wait('20');checks.push('模拟报告失败与重试恢复');
 await choose('08');await page.click('#fault');await button('连接中断').click();await wait('35');await button('恢复模拟连接').click();await wait('05');checks.push('连接异常场景与恢复模拟连接');
 await page.locator('#hostToast.visible').waitFor({state:'hidden'});await choose('01');await page.screenshot({path:path.join(out,'首页.png')});
 await choose('08');await page.screenshot({path:path.join(out,'我的教室.png')});
 for(const id of await page.locator('#scene option').evaluateAll(options=>options.map(o=>o.value))){await choose(id);const result=await page.evaluate(()=>{const doc=document.getElementById('canvas').contentDocument;return {loaded:[...doc.images].every(i=>i.complete&&i.naturalWidth>0),width:document.documentElement.scrollWidth,view:innerWidth};});assert(result.loaded,'场景图片未加载 '+id);assert(result.width<=result.view,'宿主页水平溢出 '+id);}
 const sceneCount=await page.locator('#scene option').count();checks.push(sceneCount+'个场景可到达，所有本地图片载入，无宿主页水平溢出');
 await page.setViewportSize({width:1366,height:768});await choose('08');await page.screenshot({path:path.join(out,'笔记本视口.png')});
 microphoneRequests=await page.evaluate(()=>window.__micRequests||0);assert.equal(microphoneRequests,0);assert.equal(external.length,0);assert.equal(errors.length,0);
 const result={verified:new Date().toISOString(),scope:'独立静态原型，非产品功能或模型质量验收',checks,sceneCount,externalRequests:external,microphoneRequests,errors};fs.writeFileSync(path.join(out,'acceptance.json'),JSON.stringify(result,null,2),'utf8');console.log(JSON.stringify(result,null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
