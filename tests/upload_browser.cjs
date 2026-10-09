// 用真实浏览器验证文件解码、上传界面和本机 ASR；可选验证配置好的真实课堂模型链路。
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require('playwright');

async function main() {
  const browser = await chromium.launch({headless:true, args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'], ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  const context = await browser.newContext();
  const page = await context.newPage();
  const failures = [];
  page.on('pageerror', error => failures.push(error.message));
  const base = process.env.UPLOAD_TEST_URL || 'http://127.0.0.1:8765';
  const wav = path.resolve('asr/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/test_wavs/zh.wav');
  try {
    await page.goto(base + '/models');
    await page.waitForFunction(() => typeof AudioFileInput === 'function' && audioUploadAvailable === true);
    await page.locator('#debugUploadFile').setInputFiles(wav);
    await page.waitForFunction(() => window.asrDebug.upload.phase === 'ready');
    await page.locator('#debugUploadStart').click();
    await page.waitForFunction(() => window.asrDebug.upload.phase === 'finished' && !window.asrDebug.busy, {timeout:120000});
    const realRows = await page.evaluate(() => window.asrDebug.records);
    assert(/(九|9)点/.test(await page.locator('#asrDebugResults').innerText()), JSON.stringify(realRows));
    assert(/(五|5)点/.test(await page.locator('#asrDebugResults').innerText()), JSON.stringify(realRows));
    assert.equal(await page.locator('#debugUploadProgress').evaluate(el => el.value), 100);
    assert(await page.locator('#debugUploadStart').isEnabled());
    assert(await page.evaluate(() => window.asrDebug.records.every(row => row.input_source === 'audio_file' && row.filename === 'zh.wav')));
    console.log('PASS real browser WAV → VAD → local ASR, source metadata and retained selection');

    for (const filename of (process.env.UPLOAD_EXTRA_FILES || '').split(';').filter(Boolean)) {
      await page.locator('#debugUploadFile').setInputFiles(filename);
      await page.waitForFunction(() => ['ready','error'].includes(window.asrDebug.upload.phase));
      assert.equal(await page.evaluate(() => window.asrDebug.upload.phase), 'ready', await page.locator('#debugUploadStatus').innerText());
      console.log('PASS browser decoding ' + path.basename(filename));
    }

    await page.locator('#debugUploadFile').setInputFiles({name:'broken.wav',mimeType:'audio/wav',buffer:Buffer.from('not an audio file')});
    await page.waitForFunction(() => window.asrDebug.upload.phase === 'error');
    assert(await page.locator('#debugUploadStart').isDisabled());
    console.log('PASS corrupt audio does not start ASR');

    // Explicitly invoke the shared converter with controlled decoded data to test channel averaging and limits.
    const checks = await page.evaluate(async () => {
      const RealContext = AudioContext;
      let decoded = {length:3, numberOfChannels:2, sampleRate:48000, duration:3/48000,
        getChannelData:c => new Float32Array(c ? [-1,.2,.4] : [1,.6,.8])};
      window.AudioContext = class {state='running'; async decodeAudioData() {return decoded;} async close(){this.state='closed';}};
      const upload = window.asrDebug.upload;
      try {
        const file = {name:'stereo.wav',size:10,arrayBuffer:async()=>new ArrayBuffer(10)};
        await upload.select(file);
        const mono = Array.from(upload.audio.samples);
        decoded = {...decoded, duration:3600};
        await upload.select({...file,size:700*1024*1024});
        const boundaryAccepted = upload.phase === 'ready';
        decoded = {...decoded, duration:3600.1}; await upload.select(file);
        const overlong = upload.phase === 'error' && !upload.audio;
        await upload.select({...file,size:700*1024*1024+1});
        const oversized = upload.phase === 'error' && !upload.audio;
        await upload.select({...file,size:0});
        return {mono,boundaryAccepted,overlong,oversized,empty:upload.phase === 'error' && !upload.audio};
      } finally { window.AudioContext = RealContext; }
    });
    assert.deepEqual(checks.mono.map(n=>Math.round(n*10)), [0,4,6]);
    assert(checks.boundaryAccepted && checks.overlong && checks.oversized && checks.empty);
    console.log('PASS stereo downmix and empty/size/duration validation');

    // Stop during a slow ACK, without dropping the already-sent frame or sending the remainder.
    const stopped = await page.evaluate(async () => {
      const upload = window.asrDebug.upload;
      const original = upload.options;
      let frames = 0, endCancelled;
      upload.file = {name:'cancel.wav'}; upload.audio = {samples:new Float32Array(4096),sampleRate:16000,duration:.256};
      upload.options = {blocked:()=>null,readyKind:'ready',finishedText:'done',
        open:metadata => upload.handle({type:'ready',data:metadata}), send:()=>{frames++;},
        end:cancelled=>{endCancelled=cancelled; upload.handle({type:'upload_finished',data:{upload_id:upload.id,processed_samples:2048,cancelled:true}});}};
      try { await upload.start(); await upload.stop(); return {frames,endCancelled,busy:upload.busy,status:upload.message}; }
      finally {upload.options=original;}
    });
    assert.equal(stopped.frames, 1); assert(stopped.endCancelled && !stopped.busy);
    assert(stopped.status.includes('仅处理已接收部分'));
    console.log('PASS stop while waiting for ACK');

    await page.locator('#debugUploadFile').setInputFiles(wav);
    await page.waitForFunction(() => window.asrDebug.upload.phase === 'ready');
    await page.locator('#asrDebugStart').click();
    await page.waitForFunction(() => window.asrDebug.phase === 'recording');
    assert(await page.locator('#debugUploadStart').isDisabled());
    await page.locator('#asrDebugStop').click();
    await page.waitForFunction(() => !window.asrDebug.busy);
    await page.waitForFunction(() => !document.getElementById('debugUploadStart').disabled);
    console.log('PASS microphone regression and mutual exclusion');

    await page.route('**/api/health', async route => {
      const response = await route.fetch();
      const health = await response.json(); delete health.audio_upload_available;
      await route.fulfill({response,json:health});
    });
    await page.waitForFunction(() => !refreshingASR); await page.evaluate(() => refreshASR());
    assert(await page.locator('#debugUploadStart').isDisabled());
    assert((await page.locator('#debugUploadStatus').innerText()).includes('重启服务'));
    await page.unroute('**/api/health'); await page.waitForFunction(() => !refreshingASR); await page.evaluate(() => refreshASR());
    console.log('PASS older backend disables file upload with restart hint');

    if (process.env.LIVE_CLASSROOM === '1') {
      await page.locator('#classroomTab').click();
      await page.locator('#topic').fill('工作时间与工作内容');
      await page.locator('#points').fill('上午九点至下午五点的工作安排');
      await page.locator('#tts').uncheck(); await page.locator('#mute').check();
      await page.locator('#start').click();
      await page.waitForFunction(() => running, {timeout:120000});
      await page.locator('#lessonUploadFile').setInputFiles(wav);
      await page.waitForFunction(() => window.lessonUpload.phase === 'ready');
      await page.locator('#lessonUploadStart').click();
      await page.waitForFunction(() => window.lessonUpload.phase === 'finished', {timeout:120000});
      await page.waitForFunction(() => records.some(e=>e.type === 'state'), {timeout:120000});
      assert(await page.evaluate(() => records.some(e=>e.type==='transcript' && e.data.input_source==='audio_file')));
      assert(await page.evaluate(() => records.some(e=>e.type==='review_completed')));
      await page.locator('#end').click();
      await page.waitForFunction(() => !running && !ending);
      assert.deepEqual(await page.evaluate(() => records.find(e=>e.type==='finished').data.unprocessed_sources), []);
      console.log('PASS real classroom upload → review → student → saved classroom record');
    }
    await page.screenshot({path:process.env.UPLOAD_SCREENSHOT || '/tmp/learnfromyou-upload.png',fullPage:true});
    assert.deepEqual(failures, []);
  } finally {await context.close(); await browser.close();}
}
main().catch(error => {console.error(error); process.exitCode = 1;});
