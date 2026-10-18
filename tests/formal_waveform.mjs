// 确定性时钟验证真实音量映射、静音衰减、重绘绑定及动画资源释放。
import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
const source=await readFile(new URL('../frontend/formal/waveform.js',import.meta.url),'utf8');
const {MicrophoneWaveform}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
let now=0,id=0;
const frames=new Map();
globalThis.performance={now:()=>now};
globalThis.document={hidden:false};
globalThis.requestAnimationFrame=callback=>{frames.set(++id,callback);return id;};
globalThis.cancelAnimationFrame=token=>frames.delete(token);
const element=()=>({isConnected:true,classList:{toggle(){}},bars:Array.from({length:35},()=>({style:{}})),querySelectorAll(){return this.bars;}});
const wave=new MicrophoneWaveform(),first=element();
wave.bind(first);
const advance=(ms,rms)=>{
  for(let elapsed=0;elapsed<ms;elapsed+=20){
    now+=20;if(rms!==undefined)wave.input(rms,true);
    const pending=[...frames.values()];frames.clear();for(const callback of pending)callback(now);
    assert.ok(frames.size<=1,'最多保留一个动画循环');
  }
};
const max=el=>Math.max(...el.bars.map(bar=>parseFloat(bar.style.height)));
advance(500,0);assert.equal(max(first),4);
advance(500,0.002);assert.equal(max(first),4,'低于噪声底线不跳动');
advance(1000,0.02);const quiet=max(first);assert.ok(quiet>4);
advance(1000,0.2);assert.ok(max(first)>quiet);assert.ok(max(first)<=37);
const replacement=element();wave.bind(replacement);
assert.equal(max(replacement),max(first),'页面刷新不丢失音量历史');
advance(4000);assert.equal(max(replacement),4,'停止收到音频后回落，不能冻结在旧音量');
advance(1000,0.2);wave.input(0,false);
assert.equal(max(replacement),4);assert.equal(frames.size,0,'暂停立即复位并取消动画');
advance(300,0.1);wave.bind(null);assert.equal(frames.size,0);
advance(300,0.1);assert.equal(frames.size,0,'其他页面不绘制波形');
wave.bind(element());assert.equal(frames.size,1);advance(400,0.1);
document.hidden=true;wave.sync();assert.equal(frames.size,0);
document.hidden=false;wave.sync();assert.equal(frames.size,1);
wave.element.isConnected=false;advance(40);assert.equal(frames.size,0);
console.log('formal waveform: volume, noise floor, decay, rebind, pause, navigation and single loop passed');
