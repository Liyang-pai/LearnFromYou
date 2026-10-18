'use strict';
// 真实入口 + Web Audio 合成输入，离线覆盖主/备用采集与课堂页面生命周期。
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../frontend'),base='http://127.0.0.1:18979',sid='012345abcdef';
const realMic=process.env.REAL_MIC==='1';
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH});
  try {
    for(const fallback of realMic?[false]:[false,true]) {
      const page=await browser.newPage({viewport:{width:1600,height:1100}}),errors=[],unexpected=[];
      let socket,audioFrames=0,starts=0,stops=0;
      page.on('pageerror',error=>errors.push(error.message));
      if(realMic)await page.context().grantPermissions(['microphone'],{origin:base});
      else await page.addInitScript(fallback=>{
        if(fallback)AudioWorklet.prototype.addModule=async()=>{throw new Error('模拟备用采集');};
        navigator.mediaDevices.getUserMedia=async()=>{
          const context=new AudioContext(),oscillator=context.createOscillator(),gain=context.createGain(),output=context.createMediaStreamDestination();
          oscillator.frequency.value=220;gain.gain.value=0;
          oscillator.connect(gain);gain.connect(output);oscillator.start();await context.resume();
          window.syntheticMic={context,gain};
          for(const track of output.stream.getTracks()){
            const stop=track.stop.bind(track);track.stop=()=>{stop();oscillator.stop();void context.close();};
          }
          return output.stream;
        };
      },fallback);
      await page.routeWebSocket(base.replace('http:','ws:')+'/ws/session',route=>{
        socket=route;
        route.onMessage(raw=>{
          if(typeof raw!=='string'){audioFrames++;return;}
          const event=JSON.parse(raw);
          if(event.type==='start')route.send(JSON.stringify({type:'ready',data:{session_id:sid,lesson:event.lesson,muted:false}}));
          if(event.type==='audio_start')starts++;
          if(event.type==='audio_stop')stops++;
          if(event.type==='mute')route.send(JSON.stringify({type:'mute',data:{muted:event.value}}));
          if(event.type==='end')route.send(JSON.stringify({type:'finished',data:{}}));
        });
      });
      await page.route('**/*',async route=>{
        const url=new URL(route.request().url());
        if(url.origin!==base || route.request().method()!=='GET'){unexpected.push(url.href);return route.abort();}
        if(url.pathname==='/api/health')return route.fulfill({json:{asr_ready:true,model_configured:true}});
        if(url.pathname==='/api/settings')return route.fulfill({json:{tts:false,asr_review:false,model_configured:true}});
        if(url.pathname==='/api/history')return route.fulfill({json:{items:[],total:0,indexing:false}});
        if(url.pathname===`/api/teaching-report/${sid}`)return route.fulfill({json:{session_id:sid,status:'idle'}});
        if(url.pathname.startsWith('/api/')){unexpected.push(url.href);return route.abort();}
        const file=path.resolve(root,url.pathname.startsWith('/static/')?decodeURIComponent(url.pathname.slice(8)):'formal/index.html');
        assert.ok(file.startsWith(root+path.sep));
        const contentType={'.html':'text/html','.js':'text/javascript','.css':'text/css','.png':'image/png','.svg':'image/svg+xml','.woff2':'font/woff2','.wav':'audio/wav'}[path.extname(file)];
        await route.fulfill({body:await fs.readFile(file),contentType});
      });
      await page.goto(base+'/create');
      await page.locator('#setup-topic').fill('实时波形验证');
      await page.locator('#setup-student_profile').fill('尚未学习一次函数。');
      await page.locator('[data-action="create"]').click();
      await page.waitForURL(base+'/classroom');
      const heights=()=>page.locator('[data-mic-wave] i').evaluateAll(bars=>bars.map(bar=>parseFloat(bar.style.height)));
      const max=async()=>Math.max(...await heights());
      const volume=level=>page.evaluate(level=>{window.syntheticMic.gain.gain.value=level;},level);
      const waitLoud=()=>page.waitForFunction(()=>Math.max(...[...document.querySelectorAll('[data-mic-wave] i')].map(bar=>parseFloat(bar.style.height)))>20);
      assert.equal(await page.locator('[data-mic-wave] i').count(),35);
      assert.equal(await max(),4);
      await page.locator('[data-action="toggle-mic"]').click();
      await page.locator('[data-mic-wave].active').waitFor({timeout:realMic?8000:30000});
      if(realMic){
        const observed=[];
        for(let i=0;i<12;i++){await page.waitForTimeout(250);observed.push(await max());}
        await page.locator('[data-action="toggle-mic"]').click();
        await page.locator('[data-mic-wave]:not(.active)').waitFor();assert.equal(await max(),4);
        assert.ok(audioFrames>0,'真实设备必须产生音频帧');
        assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
        console.log(`formal waveform real microphone: ${audioFrames} frames, observed bar range ${Math.min(...observed)}–${Math.max(...observed)} px, pause reset passed (speech not assumed)`);
        await page.close();continue;
      }
      await page.waitForTimeout(300);assert.equal(await max(),4);
      await volume(0.02);await page.waitForTimeout(700);const quiet=await max();assert.ok(quiet>4 && quiet<20);
      await volume(0.2);await waitLoud();assert.ok(await max()<=37);
      // 音量变化不得替换页面/聊天 DOM；一般状态刷新后需绑定新容器。
      await page.evaluate(()=>{window.originalChat=document.querySelector('.chat-body');});
      await page.waitForTimeout(250);
      assert.equal(await page.evaluate(()=>window.originalChat===document.querySelector('.chat-body')),true);
      socket.send(JSON.stringify({type:'reply',data:{text:'我听到了，请继续讲。'}}));
      await page.getByText('我听到了，请继续讲。',{exact:true}).first().waitFor();
      await waitLoud();assert.equal(await page.locator('[data-mic-wave]').count(),1);
      await page.locator('[data-action="toggle-muted"]').click();
      await page.getByText('恢复学生发言',{exact:true}).waitFor();await waitLoud();
      await volume(0);await page.waitForFunction(()=>[...document.querySelectorAll('[data-mic-wave] i')].every(bar=>parseFloat(bar.style.height)===4));
      await volume(0.2);await waitLoud();
      await page.locator('[data-action="toggle-mic"]').click();
      await page.locator('[data-mic-wave]:not(.active)').waitFor();assert.equal(await max(),4);
      await page.locator('[data-action="toggle-mic"]').click();await page.locator('[data-mic-wave].active').waitFor();
      await volume(0.2);await waitLoud();
      const beforeLeave=audioFrames;
      await page.locator('.sidebar a[href="/settings"]').click();await page.waitForURL(base+'/settings');
      assert.equal(await page.locator('[data-mic-wave]').count(),0);await page.waitForTimeout(250);
      assert.ok(audioFrames>beforeLeave,'切页后继续原有录音采集');
      await page.locator('.sidebar a[href="/classroom"]').click();await page.waitForURL(base+'/classroom');await waitLoud();
      await page.screenshot({path:`/tmp/formal-waveform-${fallback?'fallback':'worklet'}.png`});
      socket.send(JSON.stringify({type:'error',data:{code:'audio_failure',message:'模拟录音故障'}}));
      await page.locator('[data-mic-wave]:not(.active)').waitFor();assert.equal(await max(),4);
      await page.locator('[data-action="toggle-mic"]').click();await page.locator('[data-mic-wave].active').waitFor();
      await volume(0.2);await waitLoud();
      if(fallback){
        await page.locator('[data-action="end-class"]').click();
        await page.locator('#action-confirm .modal-footer button').last().click();
        await page.waitForURL(base+`/report/${sid}`);
        assert.equal(await page.locator('[data-mic-wave]').count(),0);
        assert.equal(stops,3);
        assert.equal(await page.evaluate(()=>window.syntheticMic.context.state),'closed');
      }else{
        socket.close();await page.locator('[data-mic-wave]:not(.active)').waitFor();assert.equal(await max(),4);
      }
      assert.ok(audioFrames>0);assert.equal(starts,3);assert.ok(stops>=2);
      assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
      await page.close();
      console.log(`formal waveform browser: ${fallback?'fallback':'worklet'} audio, silence, reply, mute, pause, navigation, failure and ${fallback?'class end':'disconnect'} passed`);
    }
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
