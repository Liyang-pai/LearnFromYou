'use strict';
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {pathToFileURL}=require('url'),{chromium}=require('playwright');
(async()=>{
 const root=path.resolve(__dirname,'..'),browser=await chromium.launch({headless:true,executablePath:process.env.DESIGN_BROWSER_PATH});
 try{
 const context=await browser.newContext({offline:true,acceptDownloads:true,viewport:{width:1800,height:1200}}),page=await context.newPage();
 const errors=[],external=[],checks=[];page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(/^(https?|wss?):/.test(r.url()))external.push(r.url());});
 await page.addInitScript(()=>{navigator.mediaDevices.getUserMedia=()=>{window.__micRequests=(window.__micRequests||0)+1;return Promise.reject(Error('不允许采集'));};});
 const frame=()=>page.frameLocator('#canvas'),button=name=>frame().getByRole('button',{name,exact:true});
 const ready=async id=>page.waitForFunction(id=>document.getElementById('canvas').contentDocument?.body?.dataset.prototypeReady===id,id);
 const choose=async id=>{await page.selectOption('#scene',id);await ready(id);};
 const state=()=>page.evaluate(()=>window.prototypeApp.state);
 await page.goto(pathToFileURL(path.join(root,'index.html')).href);await ready('01');
 for(const id of ['29','30']){await choose(id);await frame().locator('.main>.detail-tabs span').filter({hasText:'ASR 模型'}).click();await ready('30');await frame().locator('.main>.detail-tabs span').filter({hasText:'试讲课堂'}).click();await ready('29');}
 checks.push('两个调试页的两个主页签共4条路由均正确');
 await button('创建试讲').last().click();await ready('29');assert((await state()).session.active);
 await button('开启麦克风').click();await ready('29');assert((await state()).session.mic);await button('暂停麦克风').click();await ready('29');assert(!(await state()).session.mic);
 await button('学生静音').click();await ready('29');assert((await state()).session.muted);await button('恢复学生发言').click();await ready('29');assert(!(await state()).session.muted);
 await button('关闭语音播报').click();await ready('29');assert(!(await state()).settings.tts);await button('语音播报').click();await ready('29');assert((await state()).settings.tts);
 checks.push('调试课堂创建、麦克风、静音、播报开关有状态变化并留在调试页');
 for(const [label,key] of [['转写与片段','records'],['模型输入','messages'],['学生状态','knowledge'],['模型与事件','events']]){await frame().locator('.debug-grid .detail-tabs span').filter({hasText:label}).click();await ready('29');await frame().locator('.debug-console').filter({hasText:key}).waitFor();}
 checks.push('4个调试详情页签切换内容');
 const chooser=page.waitForEvent('filechooser');await button('确认教学截图').click();await(await chooser).setFiles(path.join(root,'assets','小纸-用户原图.png'));await ready('14');await button('确认上传').click();await ready('05');assert.equal((await state()).image.name,'小纸-用户原图.png');
 checks.push('调试截图入口打开选择器→本地预览→确认');
 await choose('13');await button('全部事件').click();await frame().getByRole('dialog',{name:'课堂事件筛选'}).waitFor();await button('只看异常').click();assert.equal((await state()).eventFilter,'异常');await frame().getByText('当前没有符合条件的事件。',{exact:true}).waitFor();await button('全部事件').last().click();assert.equal((await state()).eventFilter,'全部');await button('关闭').last().click();
 checks.push('全部事件弹窗可筛选并恢复全部事件');
 await choose('20');const download=page.waitForEvent('download');await button('导出日志').click();const file=await download;const exported=JSON.parse(fs.readFileSync(await file.path(),'utf8'));assert(exported.lesson.topic);assert(exported.messages);assert(!JSON.stringify(exported).includes('sources'));
 assert.equal(await page.locator('#scene option[value="28"]').count(),0);checks.push('数据与诊断页已移除，报告页可下载本次课堂日志');
 await page.evaluate(()=>{window.prototypeApp.state.session.active=false;});await choose('24');
 await frame().locator('.model-card').filter({hasText:'SenseVoice Small'}).getByRole('button',{name:'详情 / 来源与许可'}).click();await ready('38');await frame().getByRole('heading',{name:'SenseVoice Small',exact:true}).waitFor();await frame().getByText('asr/models/sensevoice-small-int8/',{exact:true}).waitFor();
 await button('模型来源').click();assert((await frame().getByLabel('模型来源地址').inputValue()).includes('sense-voice'));assert((await frame().getByLabel('许可地址').inputValue()).endsWith('MODEL_LICENSE'));await button('关闭').last().click();
 await button('权重许可').click();await frame().getByLabel('许可地址').waitFor();await button('关闭').last().click();checks.push('模型详情对应所点模型，来源、版本、许可地址可查看');
 for(const name of ['校验 / 修复','准备 VAD']){await button(name).click();await frame().getByRole('dialog',{name:name+'（模拟）'}).waitFor();await frame().getByText(name+'完成。'+(name==='准备 VAD'?'模拟 VAD 已就绪。':'模拟文件检查已通过。'),{exact:true}).waitFor();await button('关闭').last().click();}
 assert((await state()).vadReady);await button('重新下载').click();await ready('25');await ready('24');assert((await state()).installed.includes('Whisper Small'));checks.push('校验、VAD准备和重新下载均有模拟过程与结果');
 await choose('14');await frame().getByRole('button',{name:'关闭',exact:true}).focus();await page.keyboard.press('Enter');await ready('08');checks.push('SVG关闭入口支持键盘Enter');
 assert.equal(errors.length,0);assert.equal(external.length,0);assert.equal(await page.evaluate(()=>window.__micRequests||0),0);
 const result={date:new Date().toISOString(),checks,errors,externalRequests:external,microphoneRequests:0};fs.writeFileSync(path.join(root,'output','repair-regression.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
