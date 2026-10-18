'use strict';
// 真实页面脚本 + 离线接口，验证正式侧栏清理及调试页面恢复。
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../frontend');
const base = 'http://127.0.0.1:18979', sid = '012345abcdef';

(async () => {
  const browser = await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [], unexpected = [], debugAssets = [];
    let reportPosts = 0;
    let classroomSocket;
    await page.addInitScript(() => {
      window.notificationStarts=0;
      const create=AudioContext.prototype.createBufferSource;
      AudioContext.prototype.createBufferSource=function(...args){
        const sound=create.apply(this,args),start=sound.start;
        sound.start=function(...args){window.notificationStarts++;return start.apply(this,args);};
        return sound;
      };
    });
    await page.routeWebSocket(base.replace('http:','ws:')+'/ws/session', socket => {
      classroomSocket=socket;
      socket.onMessage(raw=>{
        const event=JSON.parse(raw);
        if(event.type==='start')socket.send(JSON.stringify({type:'ready',data:{session_id:sid,lesson:event.lesson,muted:false}}));
      });
    });
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url()), method = route.request().method();
      if (url.origin !== base) { unexpected.push(url.href); return route.abort(); }
      if (url.pathname === `/api/teaching-report/${sid}`) {
        if (method === 'POST') reportPosts++;
        return route.fulfill({json:{session_id:sid,status:method==='POST'?'failed':'idle',progress:'可以手动生成报告',error:method==='POST'?'离线模拟报告失败':''}});
      }
      if (method !== 'GET') { unexpected.push(method+' '+url.href); return route.abort(); }
      if (url.pathname === '/api/health') return route.fulfill({json:{asr_ready:false,model_configured:false,asr_debug_available:true,audio_upload_available:true,image_upload_available:true,
        runtime:{capabilities:['question_release_v1'],rules_fingerprint:'offline-fixture',pid:0}}});
      if (url.pathname === '/api/settings') return route.fulfill({json:{tts:true,asr_review:true,model_configured:false}});
      if (url.pathname === '/api/asr/models') return route.fulfill({json:{models:[],models_dir:'offline/models',asr_ready:false,vad_ready:false,busy:false}});
      if (url.pathname === '/api/history') return route.fulfill({json:{items:[{session_id:sid,topic:'离线历史主题',grade:'高中',subject:'语文'}],total:1,indexing:false}});
      if (url.pathname === `/api/history/${sid}`) return route.fulfill({json:{status:'ready',topic:'离线历史主题',warnings:[],student_profile:'回归画像',report_unavailable:''}});
      if (url.pathname === `/api/history/${sid}/dialogue`) return route.fulfill({json:{status:'ready',items:[{role:'teacher',text:'真实讲授',raw_text:'真实讲授',request_count:1}],total:1,before:null,revision:'offline'}});
      if (url.pathname.startsWith('/api/')) { unexpected.push(url.href); return route.abort(); }
      const relative = url.pathname.startsWith('/static/') ? decodeURIComponent(url.pathname.slice(8)) : url.pathname.startsWith('/debug') ? 'index.html' : 'formal/index.html';
      const file = path.resolve(root, relative);
      assert.ok(file.startsWith(root+path.sep));
      if (page.url().startsWith(base+'/debug') && url.pathname.startsWith('/static/')) debugAssets.push(url.pathname);
      const contentType = {'.html':'text/html','.js':'text/javascript','.css':'text/css','.png':'image/png','.svg':'image/svg+xml','.woff2':'font/woff2','.wav':'audio/wav'}[path.extname(file)];
      return route.fulfill({body:await fs.readFile(file),contentType});
    });
    async function screenshot(name) {
      if (!process.env.FRONTEND_SCREENSHOTS) return;
      await fs.mkdir(process.env.FRONTEND_SCREENSHOTS,{recursive:true});
      await page.screenshot({path:path.join(process.env.FRONTEND_SCREENSHOTS,name+'.png'),fullPage:true});
    }
    async function checkSidebar() {
      assert.deepEqual(await page.locator('.sidebar .nav-item').allTextContents(),['首页','创建试讲','我的教室','设置']);
      assert.equal(await page.locator('.sidebar .side-current,a[href^="/debug"]').count(),0);
    }

    await page.goto(base+'/classroom');
    await page.locator('.sidebar').waitFor();
    for (const variant of ['empty','ready','recording','reply','muted','failed','error','ended']) {
      await page.evaluate(async variant => {
        const {state,emptySession} = await import('/static/formal/service.js');
        const {render} = await import('/static/formal/views.js');
        const s = state.session = emptySession();
        if (variant !== 'empty') Object.assign(s,{id:'012345abcdef',lesson:{topic:'师说第一段',grade:'高一',subject:'语文'},active:true});
        if (variant === 'recording') s.mic = true;
        if (variant === 'reply') s.dialogue = [{role:'student',text:'我在听。'}];
        if (variant === 'muted') s.muted = true;
        if (['failed','error'].includes(variant)) { s.error = '离线模拟课堂错误'; s.active = variant === 'error'; }
        if (variant === 'ended') { s.active = false; s.ended = true; s.status = '本次试讲已结束'; }
        document.querySelector('#view').innerHTML = render(state,'/classroom');
      },variant);
      await checkSidebar();
      if (variant !== 'empty') assert.match(await page.locator('.page-head p').textContent(),/师说第一段/);
      if (variant === 'ready') await screenshot('formal-classroom');
    }
    await page.evaluate(async () => {
      const {state} = await import('/static/formal/service.js');
      const {render} = await import('/static/formal/views.js');
      document.querySelector('#view').innerHTML = render(state,'/classroom/details');
    });
    await checkSidebar();
    assert.equal(await page.locator('.review-summary strong').first().textContent(),'师说第一段');
    const templateCount = await page.evaluate(async () => {
      const {reference} = await import('/static/formal/reference.js');
      const {template,shell} = await import('/static/formal/prototype.js');
      for (const id of Object.keys(reference)) {
        const root = shell(template(id),'/');
        if (root.querySelector('.sidebar .side-current') || [...root.querySelectorAll('.sidebar .nav-item')].some(el=>el.textContent.includes('调试前端'))) throw new Error('未清理模板侧栏：'+id);
      }
      return Object.keys(reference).length;
    });

    // 创建按钮解锁真实 WebAudio，保持麦克风关闭，复用已有 WAV 验证通知播放。
    await page.goto(base+'/create');
    await page.locator('#setup-topic').fill('提示音离线回归');
    await page.locator('#setup-student_profile').fill('回归画像');
    await page.locator('[data-action="create"]').click();
    await page.waitForURL(base+'/classroom');
    await page.locator('[data-action="toggle-tts"]:enabled').waitFor();
    async function emit(type,text) { classroomSocket.send(JSON.stringify({type,data:{text}})); }
    await emit('reply','开启语音播报时不响提示音');
    await page.getByText('开启语音播报时不响提示音',{exact:true}).first().waitFor();
    assert.equal(await page.evaluate(()=>window.notificationStarts),0);
    await page.locator('[data-action="toggle-tts"]').click();
    await emit('candidate','候选不能播放');
    await emit('reply','关闭语音播报后的实际回复');
    await page.waitForFunction(()=>window.notificationStarts===1);
    await page.locator('[data-action="toggle-tts"]').click();
    await emit('reply','重新开启播报后不重复提示');
    await page.getByText('重新开启播报后不重复提示',{exact:true}).first().waitFor();
    assert.equal(await page.evaluate(()=>window.notificationStarts),1);
    classroomSocket.close();
    await page.locator('[data-action="toggle-tts"]:disabled').waitFor();

    for (const [url,visible] of [['/debug','#lessonPage'],['/debug/models','#modelsPage'],['/debug/history','#historyPage'],[`/debug/history/${sid}`,'#historyDetail']]) {
      await page.goto(base+url);
      await page.locator(visible).waitFor();
      await page.reload();
      await page.locator(visible).waitFor();
      assert.equal(await page.locator('body>header,body>main,.development-note,.intro').count(),4);
      assert.equal(await page.locator('.sidebar,.prototype-debug').count(),0);
      assert.deepEqual(await page.locator('link[rel="stylesheet"]').evaluateAll(nodes=>nodes.map(n=>new URL(n.href).pathname)),['/static/style.css']);
      assert.equal(await page.locator('header a[href="/"]').count(),1);
    }
    await page.locator('#historyDialogue .history-turn').waitFor();
    await page.locator('#retryHistoryReport').waitFor();
    assert.equal(reportPosts,0,'查看历史不能自动生成缺失报告');
    await page.locator('#retryHistoryReport').click();
    await page.waitForFunction(()=>document.querySelector('#historyReportStatus').textContent==='离线模拟报告失败');
    assert.equal(reportPosts,1,'点击后才发起离线模拟报告生成');

    await page.locator('#classroomTab').click();
    await page.waitForURL(base+'/debug');
    await page.locator('#topic').fill('保留调试表单草稿');
    for (const tab of ['transcripts','input','state','output']) {
      await page.locator(`[data-tab="${tab}"]`).click();
      assert.equal(await page.locator(`[data-tab="${tab}"]`).getAttribute('aria-selected'),'true');
      assert.ok(await page.locator('#pane-'+tab).isVisible());
    }
    await screenshot('debug-classroom');
    await page.locator('#modelsTab').click();
    await page.waitForURL(base+'/debug/models');
    for (const id of ['asrDebugStart','debugUploadFile','debugUploadStart']) assert.ok(await page.locator('#'+id).isVisible());
    await screenshot('debug-models');
    await page.locator('#historyTab').click();
    await page.locator('.history-card').waitFor();
    await page.locator('.history-card').click();
    await page.waitForURL(base+'/debug/history/'+sid);
    await page.locator('#backHistory').click();
    await page.waitForURL(base+'/debug/history');
    await page.locator('#classroomTab').click();
    assert.equal(await page.locator('#topic').inputValue(),'保留调试表单草稿');
    for (const id of ['lessonUploadFile','transcripts','modelInputs','stateJson','events','export','retry','generateTeachingReport','asrDebugExport']) {
      assert.equal(await page.locator('#'+id).count(),1,'保留调试控件 '+id);
    }
    assert.ok(!debugAssets.some(url=>url.startsWith('/static/formal/') || url.includes('debug-layout')),'调试页不能加载正式页面依赖');
    assert.deepEqual(errors,[]);
    assert.deepEqual(unexpected,[]);
    console.log(`frontend shell: ${templateCount} templates, 8 classroom states, detail theme, reply sound, debug direct/refresh/navigation and manual reports passed (offline)`);
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
