// 录音权限、迟到授权和启停生命周期回归测试，不读取真实麦克风。
import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
const source=await readFile(new URL('../frontend/formal/audio.js',import.meta.url),'utf8');
const {requestMicrophone,Microphone}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
let resolve,stopped=0;
await assert.rejects(requestMicrophone(()=>new Promise(r=>resolve=r),5),/权限超时/);
resolve({getTracks:()=>[{stop(){stopped++;}}]}); await Promise.resolve();
assert.equal(stopped,1);
const stream={getTracks:()=>[]}; assert.equal(await requestMicrophone(()=>Promise.resolve(stream)),stream);

const node=()=>({connect(){},disconnect(){}});
const tracks=[],contexts=[];
const mediaStream=()=>{const track={stopped:false,stop(){this.stopped=true;}};tracks.push(track);return {getTracks:()=>[track]};};
let getMedia=async()=>mediaStream();
globalThis.AudioContext=class {
  constructor(){this.state='running';this.sampleRate=16000;this.destination={};this.audioWorklet={addModule:async()=>{throw new Error('使用离线后备音频图');}};contexts.push(this);}
  async resume(){}
  createMediaStreamSource(){return node();}
  createGain(){return {...node(),gain:{value:1}};}
  createScriptProcessor(){return node();}
  async close(){this.state='closed';}
};
Object.defineProperty(globalThis,'navigator',{value:{mediaDevices:{getUserMedia:()=>getMedia()}},configurable:true});
globalThis.WebSocket={OPEN:1};
const messages=[],socket={readyState:1,bufferedAmount:0,send(message){messages.push(message);}};

// 暂停尚未完成时再次开始：新采集必须等旧图关闭，且后续仍能找到并停止新音轨。
let finishOldClose;
const mic=new Microphone();
mic.active=true;mic.stream=mediaStream();
mic.context={state:'running',close:()=>new Promise(resolve=>{finishOldClose=resolve;})};
const beforeRestart=tracks.length,stopping=mic.stop(null,null),restarting=mic.start(socket);
await Promise.resolve();
assert.equal(tracks.length,beforeRestart,'旧图关闭前不能申请新的麦克风');
finishOldClose();await Promise.all([stopping,restarting]);
assert.equal(mic.active,true);assert.ok(mic.stream);assert.ok(mic.context);
const currentTrack=tracks.at(-1);assert.equal(currentTrack.stopped,false);
await mic.stop(socket);assert.equal(currentTrack.stopped,true);assert.equal(mic.active,false);assert.equal(mic.stream,null);

// 等待权限期间结束：迟到的音轨必须释放，不能再发送 audio_start。
let grant;
getMedia=()=>new Promise(resolve=>{grant=resolve;});
const cancelled=new Microphone(),messageCount=messages.length;
const starting=cancelled.start(socket);
while(!grant)await Promise.resolve();
await cancelled.stop(null,null);
const late=mediaStream();grant(late);
await assert.rejects(starting,/录音准备已取消/);
assert.equal(tracks.at(-1).stopped,true);assert.equal(messages.length,messageCount);
assert.equal(cancelled.active,false);assert.equal(cancelled.context,null);assert.equal(cancelled.busy,false);
assert.ok(contexts.every(context=>context.state==='closed'));

// 音量回调兼容已有 start 参数；备用路径计算 RMS，不改变 PCM 发送内容。
getMedia=async()=>mediaStream();
const levels=[],meter=new Microphone({onLevel:(rms,active)=>levels.push([rms,active])});
await meter.start(socket);
const feed=samples=>meter.processor.onaudioprocess({inputBuffer:{getChannelData:()=>new Float32Array(samples)}});
feed([0,0]);feed([0.02,-0.02]);feed([0.2,-0.2]);
assert.equal(levels[1][0],0);
assert.ok(Math.abs(levels[2][0]-0.02)<1e-7);
assert.ok(levels[3][0]>levels[2][0]);
assert.deepEqual([...new Float32Array(messages.at(-1))],[Math.fround(0.2),Math.fround(-0.2)]);
const lateFrame=meter.processor.onaudioprocess;
await meter.stop(socket);
const afterStop=levels.length;
lateFrame({inputBuffer:{getChannelData:()=>new Float32Array([1])}});
assert.equal(levels.length,afterStop);
assert.deepEqual(levels.at(-1),[0,false]);

// 主路径直接复用 worklet 音量；flush 保留发送但不能重新点亮已暂停的波形。
const baseContext=globalThis.AudioContext;
globalThis.AudioContext=class extends baseContext {constructor(){super();this.audioWorklet.addModule=async()=>{};}};
globalThis.AudioWorkletNode=class {
  constructor(){this.port={postMessage:()=>{
    this.port.onmessage({data:{samples:new Float32Array([0.1]),rms:0.1}});
    this.port.onmessage({data:{flushed:true}});
  }};}
  connect(){} disconnect(){}
};
const primaryLevels=[],primary=new Microphone({onLevel:(rms,active)=>primaryLevels.push([rms,active])});
await primary.start(socket);
primary.worklet.port.onmessage({data:{samples:new Float32Array([0.8]),rms:0.05}});
assert.deepEqual(primaryLevels.at(-1),[0.05,true],'主路径应复用 worklet 的 RMS');
await primary.stop(socket);
assert.ok(primaryLevels.slice(2).every(([rms,active])=>rms===0 && !active));
assert.equal(messages.at(-1),JSON.stringify({type:'audio_stop'}));
const broken=new Microphone({onLevel:()=>{throw new Error('可视化异常');}});
await broken.start(socket);
broken.worklet.port.onmessage({data:{samples:new Float32Array([0.1]),rms:0.1}});
assert.equal(broken.frames,1,'音量回调异常不能阻断录音发送');
await broken.stop(socket);
console.log('formal audio: permission timeout, late cleanup, serialized restart and cancelled preparation passed');
