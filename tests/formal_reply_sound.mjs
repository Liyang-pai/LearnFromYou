// 提示音缓存、异步设置变化与声音故障回归；不使用真实设备或网络。
import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
const source=await readFile(new URL('../frontend/formal/audio.js',import.meta.url),'utf8');
const load=key=>import('data:text/javascript;base64,'+Buffer.from(source).toString('base64')+'#'+key);
let fetched=0,decoded=0,played=0,resumed=0,playbackFailure=false,resumeFailure=false;
globalThis.AudioContext=class {
  state='running';destination={};
  async resume(){resumed++;if(resumeFailure)throw new Error('声音权限受限');}
  async decodeAudioData(){decoded++;return {};}
  createBufferSource(){return {connect(){},disconnect(){},start(){if(playbackFailure)throw new Error('播放失败');played++;}};}
};
globalThis.fetch=async url=>{assert.equal(url,'/static/sounds/received-message.wav');fetched++;return {ok:true,arrayBuffer:async()=>new ArrayBuffer(1)};};
const sound=await load('cache');
await sound.playReplySound(()=>false);assert.equal(fetched,0);assert.equal(resumed,0);
await sound.playReplySound(()=>true);await sound.playReplySound(()=>true);
assert.equal(played,2);assert.equal(fetched,1);assert.equal(decoded,1);
playbackFailure=true;await sound.playReplySound(()=>true);assert.equal(played,2);playbackFailure=false;

const delayed=await load('delayed');
let finishFetch,allowed=true;
globalThis.fetch=()=>new Promise(resolve=>{finishFetch=()=>resolve({ok:true,arrayBuffer:async()=>new ArrayBuffer(1)});});
const pending=delayed.playReplySound(()=>allowed);
while(!finishFetch)await Promise.resolve();
allowed=false;finishFetch();await pending;
assert.equal(played,2,'加载期间打开播报、静音或切换课堂后，不应再响提示音');

const failed=await load('failure');
globalThis.fetch=async()=>({ok:false});
await failed.playReplySound(()=>true);assert.equal(played,2);
globalThis.fetch=async()=>({ok:true,arrayBuffer:async()=>new ArrayBuffer(1)});
await failed.playReplySound(()=>true);assert.equal(played,3,'提示音加载失败后应允许下次回复重试');
resumeFailure=true;
await (await load('blocked')).playReplySound(()=>true);assert.equal(played,3);
console.log('formal reply sound: cache, disabled/delayed guards, load retry and permission/playback failure isolation passed');
