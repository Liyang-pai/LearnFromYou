'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const data = process.env.HISTORY_TEST_DATA || fs.mkdtempSync(path.join(os.tmpdir(), 'lfy-history-browser-'));
const port = Number(process.env.HISTORY_TEST_PORT || 8894), base = `http://127.0.0.1:${port}`;
const python = process.env.LFY_TEST_PYTHON || path.join(root, '.venv/bin/python');
(async () => {
  const server = spawn(python, ['tests/history_server.py', '--data', data, '--port', String(port)], {cwd:root, stdio:'pipe'});
  let output = ''; server.stderr.on('data', d => output += d);
  let browser;
  try {
    for (let i=0;i<100;i++) { if(server.exitCode!==null) throw Error(output); try {if((await fetch(base)).ok) break;}catch{} await new Promise(r=>setTimeout(r,100)); }
    browser = await chromium.launch({executablePath:process.env.CHROME_PATH,headless:true,args:['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream']});
    const page = await browser.newPage({viewport:{width:1440,height:1000}}), errors=[], failures=[];
    page.on('pageerror', e=>errors.push(e.message));
    page.on('response', r=>{if(r.status()>=400) failures.push(r.url()+':'+r.status());});
    let audioFrames=0,closed=0,socketRoutes=0;const socketMessages=[];
    await page.routeWebSocket(`ws://127.0.0.1:${port}/ws/session`, ws=>{
      socketRoutes++;ws.onClose(()=>closed++);
      ws.onMessage(raw=>{
        if(typeof raw!=='string'){audioFrames++;return;}
        socketMessages.push(raw);
        const message=JSON.parse(raw);
        if(message.type==='start')ws.send(JSON.stringify({type:'ready',data:{session_id:'111111abcdef',asr_review:false,scope:{scope:['测试'],boundary_note:'测试'},state:{version:0,knowledge:[],open_questions:[],recent_events:[]}}}));
        if(message.type==='audio_start')ws.send(JSON.stringify({type:'audio',data:{recording:true}}));
      });
    });
    await page.goto(base);
    // Existing live containers retain their identity and state across tab navigation.
    await page.evaluate(()=>{window.__liveNode=document.getElementById('lessonPage');document.getElementById('teacherText').value='切换页面必须保留的讲课草稿';});
    await page.locator('#historyTab').click();
    await page.locator('.history-card').first().waitFor();
    assert.equal(await page.locator('.history-card').count(),process.env.HISTORY_REPLAY_SID ? 3 : 2);
    assert.equal(await page.locator('.history-card').first().locator('strong,span,time').count(),3);
    const card=page.locator('.history-card').filter({hasText:'链表历史验收'});
    await card.click();
    await page.getByText('当时确认的画像：已知变量，不知道链表。',{exact:true}).waitFor();
    await page.waitForFunction(()=>document.querySelectorAll('.history-turn').length===50);
    assert.equal(await page.locator('.history-turn').count(),50);
    await page.getByText('教学报告已生成。',{exact:true}).waitFor({timeout:15000});
    while(await page.locator('.history-turn').count()<130) {
      const previous=await page.locator('.history-turn').count();
      await page.locator('#earlierHistory').click();
      await page.waitForFunction(n=>document.querySelectorAll('.history-turn').length>n,previous);
    }
    assert.equal(await page.locator('.history-turn').count(),130);
    assert((await page.locator('.history-turn').first().innerText()).includes('链表由节点组成'));
    assert((await page.locator('.history-turn details').first().innerText()).includes('查看原始转写'));
    const download = page.waitForEvent('download'); await page.locator('#downloadHistory').click();
    assert.equal(fs.readFileSync(await (await download).path(),'utf8'),fs.readFileSync(path.join(data,'logs/012345abcdef.jsonl'),'utf8'));
    const requests=()=>JSON.parse(fs.readFileSync(path.join(data,'usage.json'))).requests.length;
    const firstBatch=page.locator('.history-turn.teacher').first();
    assert((await firstBatch.locator(':scope > small').innerText()).includes('讲授批次'));
    assert.equal(await firstBatch.locator(':scope > .history-text').textContent(),'链表由节点组成。\n沿 next 指针逐个访问节点。\n最后一个节点指向空。');
    await firstBatch.locator('summary').click();
    await page.waitForFunction(()=>document.querySelector('.history-turn.teacher details').querySelectorAll('.history-original').length===3);
    assert((await firstBatch.locator('.history-original').first().innerText()).includes('练表由节点组成'));
    assert((await page.locator('#historyDialogue').innerText()).includes('学生处理失败'));
    assert((await page.locator('.history-turn.teacher').last().innerText()).includes('发送批次未确认'));
    const count=requests(); await page.reload(); await page.getByText('教学报告已生成。',{exact:true}).waitFor(); assert.equal(requests(),count);
    await page.locator('#classroomTab').click();
    assert.equal(await page.locator('#teacherText').inputValue(),''); // Reload intentionally reset the live page.
    await page.evaluate(()=>{window.__liveNode=document.getElementById('lessonPage');document.getElementById('teacherText').value='保留草稿';});
    await page.locator('#historyTab').click(); await page.locator('#modelsTab').click(); await page.locator('#classroomTab').click();
    assert(await page.evaluate(()=>window.__liveNode===document.getElementById('lessonPage')));
    assert.equal(await page.locator('#teacherText').inputValue(),'保留草稿');
    await page.goto(base+'/history/fedcba543210');
    await page.getByText('这堂课未保存学生画像。',{exact:true}).waitFor();
    assert((await page.locator('#historyReportStatus').innerText()).includes('未正常结束'));
    assert.equal(requests(),count);
    await page.locator('#backHistory').click(); await page.goBack(); await page.getByText('这堂课未保存学生画像。',{exact:true}).waitFor();
    // A failed job never auto-retries on refresh; a deliberate retry uses the real endpoint.
    const sid='aabbccddeeff', original=fs.readFileSync(path.join(data,'logs/012345abcdef.jsonl'),'utf8');
    fs.writeFileSync(path.join(data,`logs/${sid}.jsonl`),original.replaceAll('012345abcdef',sid));
    fs.writeFileSync(path.join(data,'fail-next-report'),'1');
    await page.goto(base+'/history/'+sid);
    await page.locator('#retryHistoryReport').waitFor();
    const failedCount=requests(); await page.reload(); await page.locator('#retryHistoryReport').waitFor(); assert.equal(requests(),failedCount);
    await page.locator('#retryHistoryReport').click(); await page.getByText('教学报告已生成。',{exact:true}).waitFor({timeout:15000});
    // Keep a real browser AudioWorklet and WebSocket running while viewing history.
    await page.route('**/api/asr/models', route=>route.fulfill({json:{asr_ready:true,busy:false,models:[],models_dir:'test',engine_state:'idle',vad_ready:true}}));
    await page.route('**/api/health', route=>route.fulfill({json:{asr_ready:true,model_configured:true,asr_debug_available:true,audio_upload_available:true,image_upload_available:true}}));
    await page.context().grantPermissions(['microphone']);
    await page.locator('#classroomTab').click();
    await page.locator('#topic').fill('页面切换中的课堂');await page.locator('#studentProfile').fill('已知加法。');
    await page.locator('#asrReview').uncheck();await page.locator('#tts').uncheck();
    await page.waitForFunction(()=>!document.getElementById('start').disabled);
    await page.locator('#start').click();
    try { await page.waitForFunction(()=>running,null,{timeout:5000}); } catch(error) { console.log(await page.evaluate(()=>({starting,running,error:document.getElementById('errorText').textContent,profile:document.getElementById('profileStatus').textContent,valid:document.getElementById('lessonForm').checkValidity()})),errors,{socketRoutes,socketMessages}); throw error; }
    await page.locator('#mic').click();await page.waitForFunction(()=>recording);
    for(let i=0;i<50 && audioFrames===0;i++)await page.waitForTimeout(100);
    assert(audioFrames>0,'microphone delivered no frames');
    const framesBefore=audioFrames;
    await page.locator('#historyTab').click();await page.locator('.history-card').first().waitFor();
    for(let i=0;i<50 && audioFrames===framesBefore;i++)await page.waitForTimeout(100);
    assert(audioFrames>framesBefore,'history navigation stopped microphone frames');
    assert.equal(closed,0);assert(await page.evaluate(()=>running&&recording));
    await page.locator('#classroomTab').click();await page.locator('#mic').click();
    await page.goto(base+'/history/'+sid);await page.getByText('教学报告已生成。',{exact:true}).waitFor();
    await page.screenshot({path:path.join(data,'history-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(data,'history-mobile.png'),fullPage:true});
    if(process.env.HISTORY_REPLAY_SID){
      const replaySid=process.env.HISTORY_REPLAY_SID;
      const expected=JSON.parse(fs.readFileSync(path.join(data,'replay-expected.json'),'utf8'));
      await page.route(`**/api/teaching-report/${replaySid}`,route=>{
        assert.equal(route.request().method(),'GET','real log replay must never generate a report');
        return route.fulfill({json:{session_id:replaySid,status:'failed',error:'本次仅核对历史批次，未重新生成报告。'}});
      });
      await page.setViewportSize({width:1440,height:1000});
      await page.goto(base+'/history/'+replaySid);
      await page.waitForFunction(()=>document.querySelectorAll('.history-turn').length>0);
      while(await page.locator('#earlierHistory').isVisible()){
        const previous=await page.locator('.history-turn').count();
        await page.locator('#earlierHistory').click();
        await page.waitForFunction(n=>document.querySelectorAll('.history-turn').length>n,previous);
      }
      const batch=page.locator('.history-turn.teacher').filter({has:page.getByText(expected.text,{exact:true})});
      assert.equal(await batch.count(),1);
      assert.equal(await batch.locator(':scope > .history-text').textContent(),expected.text);
      await batch.locator('summary').click();
      await page.waitForFunction(n=>Array.from(document.querySelectorAll('details[open]')).some(el=>el.querySelectorAll('.history-original').length===n),expected.raw.length);
      assert.deepEqual(await batch.locator('.history-original > .history-text').allTextContents(),expected.raw);
      await batch.scrollIntoViewIfNeeded();
      await page.screenshot({path:path.join(data,'real-batch-desktop.png')});
      await batch.screenshot({path:path.join(data,'real-batch-expanded.png')});
      await page.setViewportSize({width:390,height:844});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await page.screenshot({path:path.join(data,'real-batch-mobile.png')});
    }
    assert.deepEqual(errors,[]); assert.deepEqual(failures,[]);
    console.log(JSON.stringify({result:'PASS',cases:['four list fields','original profile','130 turns with submitted batches','raw transcript','original log download','cache reuse','navigation preserves draft and microphone frames','interrupted lesson','history navigation','failed report manual retry','desktop/mobile'],data}));
  } finally {if(browser) await browser.close();server.kill();if(server.exitCode===null) await new Promise(r=>server.once('exit',r));}
})().catch(e=>{console.error(e);process.exitCode=1;});
