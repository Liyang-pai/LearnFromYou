// 检查静态原型的本地载入、页面导航与离线边界，不启动产品服务。
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),{pathToFileURL}=require('url');
(async()=>{
 const root=path.resolve(__dirname,'..');fs.mkdirSync(path.join(root,'output'),{recursive:true});
 const browser=await chromium.launch({headless:true,...(process.env.DESIGN_BROWSER_PATH?{executablePath:process.env.DESIGN_BROWSER_PATH}:{})});
 const context=await browser.newContext({viewport:{width:1600,height:1100},offline:true,acceptDownloads:true});
 const page=await context.newPage(),errors=[],external=[];page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(/^https?:|^wss?:/.test(r.url()))external.push(r.url());});
 await page.goto(pathToFileURL(path.join(root,'index.html')).href);
 await page.frameLocator('#canvas').locator('.page-head h1').waitFor();
 const canvas=page.frameLocator('#canvas');
 await canvas.locator('.nav-item').filter({hasText:'创建试讲'}).click();
 await canvas.locator('h1').filter({hasText:'创建试讲'}).waitFor();
 await canvas.getByLabel('教学主题',{exact:true}).fill('惯性的概念');
 await canvas.getByRole('button',{name:'生成学生画像',exact:true}).click();
 await page.waitForFunction(()=>window.prototypeApp.screen==='03');
 await canvas.getByRole('button',{name:'确认设定，创建试讲'}).click();
 await page.waitForFunction(()=>window.prototypeApp.screen==='05');
 await canvas.getByRole('button',{name:'麦克风（模拟）'}).click();
 await page.waitForFunction(()=>window.prototypeApp.screen==='06');
 await canvas.getByLabel('讲解或提问').fill('公交车刹车时，人为什么会向前倾？');
 await canvas.getByRole('button',{name:'发送',exact:true}).click();
 await page.waitForFunction(()=>window.prototypeApp.screen==='08');
 await canvas.locator('.chat-text').filter({hasText:'静止的物体也有惯性'}).waitFor();
 await canvas.locator('.nav-item').filter({hasText:'设置'}).first().click();
 await canvas.locator('h1').filter({hasText:'设置'}).waitFor();
 await page.screenshot({path:path.join(root,'output','navigation.png')});
 const result={navigationPassed:true,createClassroomAndMockReplyPassed:true,externalRequests:external,errors};fs.writeFileSync(path.join(root,'output','smoke.json'),JSON.stringify(result,null,2));console.log(result);await browser.close();if(errors.length||external.length)process.exit(1);
})().catch(e=>{console.error(e);process.exit(1);});
