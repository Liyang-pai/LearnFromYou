'use strict';
// 真实入口与业务脚本，隔离网络并注入后端事件；不调用付费模型。
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../frontend'),base='http://127.0.0.1:18979',sid='112233aabbcc';
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],unexpected=[];
  let socket,startMode='ready',starts=0,retries=0;
  page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
  await page.routeWebSocket('**/ws/session',ws=>{
   socket=ws;ws.onMessage(raw=>{
    const e=JSON.parse(raw);
    if(e.type==='start'){
     starts++;
     if(startMode==='close')return ws.close();
     ws.send(JSON.stringify(startMode==='ready'?{type:'ready',data:{session_id:sid,lesson:e.lesson,muted:false}}:{type:'error',data:{message:startMode}}));
    }
    if(e.type==='retry'){retries++;ws.send(JSON.stringify({type:'status',data:{message:'重试请求已接收 '+retries}}));}
    if(e.type==='mute')ws.send(JSON.stringify({type:'mute',data:{muted:e.value}}));
    if(e.type==='end')ws.send(JSON.stringify({type:'finished',data:{session_id:sid}}));
   });
  });
  await page.route('**/*',async route=>{
   const url=new URL(route.request().url());
   if(url.origin!==base || route.request().method()!=='GET'){unexpected.push(route.request().method()+' '+url.href);return route.abort();}
   if(url.pathname==='/api/health')return route.fulfill({json:{asr_ready:true,model_configured:true}});
   if(url.pathname==='/api/settings')return route.fulfill({json:{tts:false,asr_review:false,model_configured:true}});
   if(url.pathname==='/api/history')return route.fulfill({json:{items:[],total:0,indexing:false}});
   if(url.pathname===`/api/teaching-report/${sid}`)return route.fulfill({json:{session_id:sid,status:'idle'}});
   if(url.pathname.startsWith('/api/')){unexpected.push(url.href);return route.abort();}
   const file=path.resolve(root,url.pathname.startsWith('/static/')?decodeURIComponent(url.pathname.slice(8)):'formal/index.html');assert.ok(file.startsWith(root+path.sep));
   return route.fulfill({body:await fs.readFile(file),contentType:{'.html':'text/html','.js':'text/javascript','.css':'text/css','.png':'image/png','.svg':'image/svg+xml','.woff2':'font/woff2','.wav':'audio/wav'}[path.extname(file)]});
  });
  async function shot(name){if(process.env.FORMAL_RECOVERY_SCREENSHOTS){await fs.mkdir(process.env.FORMAL_RECOVERY_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.FORMAL_RECOVERY_SCREENSHOTS,name+'.png'),fullPage:true});}}
  async function fill(){
   await page.locator('#setup-topic').fill('保留教学主题');await page.locator('#setup-points').fill('保留知识点');
   await page.locator('#setup-grade').selectOption('初中 · 八年级');await page.locator('#setup-subject').selectOption('物理');
   await page.locator('[data-action="setup-choice"][data-value="weak"]').click();
   await page.locator('summary').filter({hasText:'更多设置'}).click();await page.locator('#setup-textbook').fill('保留教材版本');
   await page.locator('#setup-student_profile').fill('保留学生画像');
   await page.evaluate(async()=>{const {state}=await import('/static/formal/service.js');state.session.draft='保留文字草稿';});
  }
  for(const mode of ['模型服务返回 HTTP 401','模型初始化失败','close','connect-error','close-before-open']){
   await page.goto(base+'/create');await fill();startMode=mode;
   if(mode==='connect-error')await page.evaluate(()=>{const Real=WebSocket;window.WebSocket=class extends Real{constructor(url){if(url.includes('/ws/session'))throw Error('模拟连接失败');super(url);}};});
   if(mode==='close-before-open')await page.evaluate(()=>{const Real=WebSocket;window.WebSocket=class extends Real{constructor(url){if(!url.includes('/ws/session'))return super(url);return {close(){},readyState:0,set onopen(value){},set onerror(value){},set onmessage(value){},set onclose(callback){setTimeout(()=>callback({}),0);}};}};});
   await page.locator('[data-action="create"]').click();await page.getByRole('heading',{name:'创建试讲失败',exact:true}).waitFor();
   assert.match(await page.locator('[role="alert"]').first().innerText(),mode==='close' || mode==='close-before-open'?/连接.*(断开|关闭)/:mode==='connect-error'?/模拟连接失败/:new RegExp(mode));
   assert.equal(await page.locator('[data-action="toggle-mic"],#message-form,[data-action="generate-report"]').count(),0);
   assert.ok(!(await page.locator('.main').innerText()).includes('公交车'));
   if(mode==='模型服务返回 HTTP 401')await shot('create-failure');
   const before=starts;await page.locator('[data-action="back-create"]').click();await page.waitForURL(base+'/create');
   for(const [id,value] of [['topic','保留教学主题'],['points','保留知识点'],['grade','初中 · 八年级'],['subject','物理'],['textbook','保留教材版本'],['student_profile','保留学生画像']])assert.equal(await page.locator('#setup-'+id).inputValue(),value);
   assert.equal(await page.locator('[data-action="setup-choice"][data-value="weak"]').getAttribute('class'),'selected');
   assert.equal(starts,before,'返回创建页不能自动创建');
   assert.equal(await page.evaluate(async()=>(await import('/static/formal/service.js')).state.session.draft),'保留文字草稿');
   if(mode==='模型服务返回 HTTP 401'){
    startMode='ready';await page.locator('[data-action="create"]').click();await page.locator('#teacher-draft:enabled').waitFor();
    assert.equal(await page.evaluate(async()=>(await import('/static/formal/service.js')).state.session.createError),'','同一页面重新创建成功后必须清除旧错误');
    assert.equal(await page.locator('#teacher-draft').inputValue(),'保留文字草稿');
   }
  }
  await page.goto(base+'/create');await fill();startMode='ready';
  // Wrap the actual instance after Playwright installs its socket transport.
  await page.evaluate(()=>{window.WebSocket=new Proxy(WebSocket,{construct(Type,args){const ws=new Type(...args),send=ws.send.bind(ws);ws.send=raw=>{if(window.failNextRetry && typeof raw==='string' && JSON.parse(raw).type==='retry'){window.failNextRetry=false;throw Error('模拟重试发送失败');}return send(raw);};return ws;}});});
  await page.locator('[data-action="create"]').click();await page.locator('#teacher-draft:enabled').waitFor();
  assert.equal(await page.locator('#teacher-draft').inputValue(),'保留文字草稿');
  assert.equal(await page.evaluate(async()=>(await import('/static/formal/service.js')).state.session.createError),'');
  const emit=(type,data)=>socket.send(JSON.stringify({type,data}));
  const retry=page.locator('[data-action="retry-class"]');
  emit('error',{message:'保留普通课堂错误',retryable:false});
  emit('resume_response',{status:'failed',message:'恢复调用失败',retryable:true});
  emit('knowledge_review',{status:'failed',targets:['k1'],message:'第一组复核失败',retryable:true});
  emit('knowledge_review',{status:'failed',targets:['k2'],message:'第二组复核失败',retryable:true});
  await page.getByText(/第二组复核失败/).waitFor();assert.equal(await retry.isEnabled(),true);
  assert.equal(await page.getByRole('button',{name:'重试这一段',exact:true}).count(),0,'不能残留模板中的无效重试按钮');await shot('recovery-failures');
  await page.locator('.sidebar a[href="/settings"]').click();await page.waitForURL(base+'/settings');await page.locator('.sidebar a[href="/classroom"]').click();await page.getByText(/恢复调用失败/).waitFor();
  await page.evaluate(()=>window.failNextRetry=true);await retry.click();await page.getByText('模拟重试发送失败',{exact:true}).waitFor();assert.equal(await retry.isEnabled(),true);assert.equal(retries,0);
  await page.evaluate(()=>{document.querySelector('[data-action="retry-class"]').click();document.querySelector('[data-action="retry-class"]').click();});
  await page.locator('.stage-note').filter({hasText:'重试请求已接收 1'}).waitFor({state:'attached'});assert.equal(await retry.isDisabled(),true);assert.equal(retries,1);
  await page.getByText('保留普通课堂错误',{exact:true}).waitFor();await page.getByText(/恢复调用失败/).waitFor();await shot('retry-queued');
  emit('resume_response',{status:'generated'});emit('knowledge_review',{status:'completed',targets:['k1']});
  await page.waitForFunction(()=>!document.querySelector('[data-recovery-notice]').textContent.includes('第一组复核失败'));
  assert.ok((await page.locator('[data-recovery-notice]').innerText()).includes('第二组复核失败'));assert.equal(await retry.isDisabled(),true);
  emit('knowledge_review',{status:'failed',targets:['k2'],message:'复核再次失败',retryable:true});await page.getByText(/复核再次失败/).waitFor();assert.equal(await retry.isEnabled(),true);
  emit('knowledge_review',{status:'completed',targets:['k2']});await page.waitForFunction(()=>!document.querySelector('[data-action="retry-class"]'));
  emit('resume_response',{status:'failed',message:'静音恢复失败',retryable:true});await retry.waitFor();await page.locator('[data-action="toggle-muted"]').click();await page.getByText('学生发言已暂停，恢复发言后再处理回应。',{exact:true}).waitFor();assert.equal(await retry.isDisabled(),true);
  await page.locator('[data-action="toggle-muted"]').click();await page.getByText('已提交，等待处理',{exact:true}).first().waitFor();assert.equal(await retry.isDisabled(),true);
  emit('resume_response',{status:'skipped'});await page.waitForFunction(()=>!document.querySelector('[data-action="retry-class"]'));
  // 原有讲授批次仍走相同恢复入口。
  emit('teacher_batch',{batch_id:'b1',text:'失败的讲授',request_count:1,processing_status:'failed'});emit('error',{code:'model_failure',message:'讲授处理失败',retryable:true});
  await retry.waitFor();await retry.click();await page.locator('.stage-note').filter({hasText:'重试请求已接收 2'}).waitFor({state:'attached'});assert.equal(retries,2);emit('teacher_batch',{batch_id:'b1',text:'失败的讲授',request_count:2,processing_status:'processed'});
  await page.waitForFunction(()=>!document.querySelector('[data-action="retry-class"]'));
  emit('knowledge_review',{status:'failed',targets:['k3'],message:'结束后保留错误',retryable:true});await retry.waitFor();
  await retry.click();await page.locator('.stage-note').filter({hasText:'重试请求已接收 3'}).waitFor({state:'attached'});
  await page.locator('[data-action="end-class"]').click();await page.locator('#action-confirm button[value="confirm"]').click();await page.waitForURL(base+'/report/'+sid);await page.locator('.sidebar a[href="/classroom"]').click();await page.getByText(/课堂已结束，未完成任务/).waitFor();assert.equal(await retry.isDisabled(),true);await shot('ended-failure');
  assert.ok(!(await page.locator('[data-recovery-notice]').innerText()).includes('已提交，等待处理'),'已结束课堂不能仍显示任务等待处理');
  // 新课堂断线后也保留原因并禁用重试。
  await page.locator('.sidebar a[href="/create"]').click();await page.locator('[data-action="create"]').click();await page.locator('#teacher-draft:enabled').waitFor();
  emit('resume_response',{status:'failed',message:'断线前恢复失败',retryable:true});await retry.waitFor();socket.close();await page.getByText(/连接已断开，未完成任务/).waitFor();assert.equal(await retry.isDisabled(),true);
  assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
  console.log('formal recovery browser: 5 creation failures, preserved inputs, successful recreation, simultaneous failures, send rollback, duplicate retry, target isolation, mute, batch retry, end and disconnect passed (offline)');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
