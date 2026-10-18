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
console.log('formal audio: permission timeout, late cleanup, serialized restart and cancelled preparation passed');
