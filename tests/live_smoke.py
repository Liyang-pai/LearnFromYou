"""Opt-in real DeepSeek + real local ASR integration. Server must already run."""
import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.config import PORT, MODEL_DIR


async def main():
    events=[]
    async with websockets.connect(f'ws://127.0.0.1:{PORT}/ws/session',max_size=2**22) as ws:
        async def send(**data):await ws.send(json.dumps(data,ensure_ascii=False))
        async def until(kind, timeout=100):
            async with asyncio.timeout(timeout):
                while True:
                    e=json.loads(await ws.recv());events.append(e)
                    if e['type']=='error':raise RuntimeError(e['data']['message'])
                    if e['type']==kind:return e['data']
        async def observe(seconds):
            end=asyncio.get_running_loop().time()+seconds
            collected=[]
            while (remaining:=end-asyncio.get_running_loop().time())>0:
                try:e=json.loads(await asyncio.wait_for(ws.recv(),remaining))
                except TimeoutError:break
                events.append(e);collected.append(e)
                if e['type']=='error':raise RuntimeError(e['data']['message'])
            return collected

        await send(type='start',lesson={'topic':'链表的基本概念','points':'节点、连接方式和访问起点','prerequisites':''},tts=False)
        ready=await until('ready')
        print('SESSION',ready['session_id'],flush=True)
        print('PREPARATION',json.dumps(ready['scope'],ensure_ascii=False),flush=True)
        await send(type='text',text='这节课准备讲链表，但我还没有开始讲。你能讲讲如何反转链表吗？')
        state=await until('state');reply=await until('reply')
        print('UNKNOWN_KNOWLEDGE_REPLY',reply['text'],flush=True)
        assert not any(x in reply['text'] for x in ['prev','curr','next =','三指针','递归法'])
        print('UNKNOWN_STATE',json.dumps(state['state'],ensure_ascii=False),flush=True)

        await send(type='text',text='链表由节点组成。一个节点包含数据和指向下一个节点的引用。这里的引用表示可以找到那个节点的位置。节点的位置可以分散。你目前怎么理解？')
        state=await until('state');reply=await until('reply')
        print('LEARNING_REPLY',reply['text'],flush=True)
        print('LEARNING_STATE',json.dumps(state['state'],ensure_ascii=False),flush=True)

        await send(type='text',text='从头节点开始访问，头节点就是我们保存的起点。沿着引用可以找到下一个节点；最后一个节点没有下一个节点。这说明了从哪里开始和什么时候停止。你明白了吗？')
        state=await until('state');reply=await until('reply')
        print('CLARIFICATION_REPLY',reply['text'],flush=True)
        print('CLARIFICATION_QUESTIONS',json.dumps(state['state']['questions'],ensure_ascii=False),flush=True)

        await send(type='mute',value=True);await until('mute')
        await send(type='text',text='再补充一下，空链表没有任何节点。我刚才补充了什么，你知道吗？')
        state=await until('state')
        observed=await observe(1)
        assert not any(e['type']=='reply' for e in observed)
        await send(type='mute',value=False);await until('mute')
        observed=await observe(5.5)
        assert not any(e['type']=='reply' for e in observed)
        print('MUTE_PASS state_version='+str(state['state']['version']),flush=True)

        await send(type='mute',value=True);await until('mute')
        y,rate=sf.read(MODEL_DIR/'test_wavs/zh.wav',dtype='float32')
        assert rate==16000
        samples=np.interp(np.arange(len(y)*3)/3,np.arange(len(y)),y).astype('<f4')
        await send(type='audio_start',sample_rate=48000);await until('audio')
        for i in range(0,len(samples),2048):
            await ws.send(samples[i:i+2048].tobytes())
            await asyncio.sleep(2048/48000)
        await send(type='audio_stop')
        transcript=await until('transcript')
        print('AUDIO_TRANSCRIPT',transcript['text'],'ASR_SECONDS',transcript.get('asr_seconds'),flush=True)
        assert '九点' in transcript['text'] and '五点' in transcript['text']
        await until('state')
        await send(type='end');finished=await until('finished')
        assert not finished['unprocessed_sources']
        print('PASS: real API + 48kHz PCM recording path + mute + end drain',flush=True)
        print('LOG',finished['log_file'],flush=True)


if __name__=='__main__':asyncio.run(main())
