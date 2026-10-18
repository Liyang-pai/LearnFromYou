'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../frontend'),base='http://127.0.0.1:18979',sid='112233aabbcc';
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH});try{
 const page=await browser.newPage({viewport:{width:1800,height:1120}}),errors=[],unexpected=[];let socket,starts=0;
 page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
 await page.routeWebSocket('**/ws/session',ws=>{socket=ws;ws.onMessage(raw=>{if(JSON.parse(raw).type==='start'){starts++;ws.send(JSON.stringify({type:'ready',elapsed:0,data:{session_id:sid,muted:false,state:{version:0,knowledge:[]}}}));}});});
 await page.route('**/*',async route=>{const url=new URL(route.request().url());
  if(url.pathname==='/api/health')return route.fulfill({json:{asr_ready:true,model_configured:true}});
  if(url.pathname==='/api/settings')return route.fulfill({json:{tts:true,asr_review:true,model_configured:true}});
  if(url.pathname==='/api/history')return route.fulfill({json:{items:[],total:0,indexing:false}});
  if(url.pathname===`/api/history/${sid}`)return route.fulfill({json:{status:'ready',topic:'历史课堂主题',review:{understood:['历史理解'],questions:[]}}});
  if(url.pathname===`/api/history/${sid}/dialogue`)return route.fulfill({json:{status:'ready',before:null,revision:'one',items:[{role:'teacher',text:'历史讲解正文',request_count:1,elapsed:12,segments:[{raw_text:'历史识别原文'}]}]}});
  if(url.pathname.startsWith('/api/')){unexpected.push(url.pathname);return route.abort();}
  assert.equal(url.origin,base);const file=path.resolve(root,url.pathname.startsWith('/static/')?decodeURIComponent(url.pathname.slice(8)):'formal/index.html');assert.ok(file.startsWith(root+path.sep));
  return route.fulfill({body:await fs.readFile(file),contentType:{'.html':'text/html','.js':'text/javascript','.css':'text/css','.png':'image/png','.svg':'image/svg+xml','.woff2':'font/woff2','.wav':'audio/wav'}[path.extname(file)]});});
 const shot=async name=>{if(process.env.FORMAL_RECORDS_SCREENSHOTS){await fs.mkdir(process.env.FORMAL_RECORDS_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.FORMAL_RECORDS_SCREENSHOTS,name+'.png'),fullPage:true});}};
 await page.goto(base+'/classroom/details');await page.getByText('当前没有可查看的课堂。',{exact:false}).waitFor();assert.equal(starts,0);
 await page.goto(base+'/create');await page.locator('#setup-topic').fill('惯性的概念');await page.locator('#setup-student_profile').fill('需要生活例子帮助理解');await page.locator('[data-action="create"]').click();await page.locator('#teacher-draft:enabled').waitFor();
 assert.equal(await page.locator('.student-status').count(),0);assert.equal(await page.locator('[data-action="sidebar-tab"]').count(),3);
 // Acknowledgement below reads the real event reducer, avoiding fixed waits.
 const send=async(type,data,elapsed=6)=>{const count=await page.evaluate(async()=>(await import('/static/formal/service.js')).state.session.records.length);socket.send(JSON.stringify({type,elapsed,data}));await page.waitForFunction(async n=>(await import('/static/formal/service.js')).state.session.records.length>n,count);};
 await send('transcript',{id:'t1',text:'物体具有保持原来运动状太的性质。',mode:'microphone',asr_seconds:0.2});
 await send('review_completed',{segments:[{id:'t1',raw_text:'物体具有保持原来运动状太的性质。',text:'物体具有保持原来运动状态的性质。',review_status:'reviewed',review_seconds:0.68}]});
 await send('segment',{text:'惯性是一种性质，不是一种力。',reason:'完整概念讲解',sources:['t1']});
 await send('llm_request',{phase:'student',attempt:2,body:{model:'offline',messages:[{role:'user',content:JSON.stringify({teacher_sources:[{id:'t1',text:'真实讲解正文'}],knowledge:[{id:'k1',text:'惯性是一种性质',sources:['t1']}]})}]}});
 const knowledge=['understood','tentative','unclear','conflict','invalid'].map((status,i)=>({id:'k'+i,text:['惯性是一种性质','静止的物体也有惯性','如何区分性质与力','两种说法有矛盾','只有运动的物体有惯性'][i],status,reason:'依据教师的课堂讲解',conditions:'本次课堂',sources:['t1']}));
 await send('state',{state:{version:3,knowledge,open_questions:[{text:'静止的物体有惯性吗？',status:'asked',attempts:1}],knowledge_history:[{previous:{id:'k4',status:'invalid',text:'旧说法'},change_reason:'教师明确纠正',invalidated:true}],pending_reviews:[{knowledge_id:'k1',status:'failed',reason:'需要确认例子',error:'复核未完成'}]},candidate:{text:'候选尚未实际发出'}});
 await send('llm_output',{phase:'student',attempt:2,seconds:2.31,raw:JSON.stringify({text:'模型业务结果',sources:['t1']})});await send('suppressed',{reason:'教师补充了新讲解，取消旧候选'});await send('reply',{text:'实际发出的学生问题'});
 await page.locator('#teacher-draft').fill('保留的讲解草稿');
 await page.evaluate(async()=>{const {state}=await import('/static/formal/service.js');state.session.image={url:'/static/formal/assets/小纸-用户原图.png',name:'保留截图.png'};});
 await page.locator('#sidebar-tab-transcript').click();await page.getByText('教学片段',{exact:false}).first().waitFor();assert.equal(await page.locator('#teacher-draft').inputValue(),'保留的讲解草稿');await page.locator('summary').filter({hasText:'查看识别原文'}).click();await page.getByText('物体具有保持原来运动状太的性质。',{exact:true}).first().waitFor();await shot('transcripts');
 await page.locator('#sidebar-tab-student').click();for(const label of ['课堂推断理解','推断待确认','理解障碍','矛盾待解决','已失效'])assert.ok((await page.locator('#sidebar-student').innerText()).includes(label));await shot('student');
 // Long data exercises independent scroll positions and avoids relying on content height.
 await page.evaluate(async()=>{const {state,applyEvent}=await import('/static/formal/service.js');for(let i=0;i<18;i++)applyEvent(state.session,{type:'transcript',elapsed:7,data:{text:'较长的课堂记录 '+i}});});
 await page.locator('#sidebar-tab-transcript').click();await page.locator('#sidebar-transcript').evaluate(el=>el.scrollTop=80);await page.locator('#sidebar-tab-student').click();await page.locator('#sidebar-tab-transcript').click();assert.equal(await page.locator('#sidebar-transcript').evaluate(el=>el.scrollTop),80);
 await page.locator('[data-action="class-details"]').click();await page.waitForURL('**/classroom/details');assert.equal(await page.locator('.live-tabs [role="tab"]').count(),4);
 for(const [tab,body] of [['input','真实讲解正文'],['student','知识修订历史'],['output','候选已取消或抑制'],['transcript','教学片段']]){await page.locator('#details-tab-'+tab).click();if(tab==='input')await page.locator('summary').first().click();assert.ok((await page.locator('.live-detail').innerText()).includes(body));if(tab==='input')assert.ok(!(await page.locator('pre').innerText()).includes('"t1"'));await shot('details-'+tab);}
 const downloadWait=page.waitForEvent('download');await page.locator('[data-action="export-session"]').click();const download=await downloadWait;const exported=JSON.parse(await fs.readFile(await download.path(),'utf8'));assert.ok(exported.records.some(r=>r.type==='llm_request'));assert.ok(!JSON.stringify(exported).includes('"sources"'));
 await page.locator('[data-action="back-class"]').click();await page.waitForURL(base+'/classroom');assert.equal(await page.locator('#teacher-draft').inputValue(),'保留的讲解草稿');assert.equal(await page.locator('.chip-name').innerText(),'保留截图.png');assert.equal(starts,1);
 const image=await page.locator('[data-stage-student]').elementHandle();const clock=await page.locator('[data-class-clock]').innerText();await page.waitForFunction(old=>document.querySelector('[data-class-clock]').textContent!==old,clock);assert.ok(await image.evaluate(el=>el.isConnected),'计时不得重新挂载角色');
 for(const width of [1440,1800]){await page.setViewportSize({width,height:1000});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.ok(await page.locator('#teacher-draft').isVisible());await shot('classroom-'+width);}
 socket.close();await page.getByText('连接已断开',{exact:false}).first().waitFor();const frozen=await page.locator('[data-class-clock]').innerText();await page.clock.install();await page.clock.fastForward(10000);assert.equal(await page.locator('[data-class-clock]').innerText(),frozen);
 await page.goto(base+'/history/'+sid);await page.getByText('历史讲解正文',{exact:true}).waitFor();assert.ok((await page.locator('.main').innerText()).includes('历史理解'));await page.getByText('查看识别原文',{exact:true}).click();await page.getByText('历史识别原文',{exact:true}).waitFor();
 assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);console.log('formal records browser: real projections, three sidebar tabs, four detail tabs, raw text, filtering, export, scroll/draft/image retention, single connection, clock and two viewport sizes passed');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
