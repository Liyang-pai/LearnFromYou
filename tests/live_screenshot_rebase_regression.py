"""Five-subject fixed-profile regression, real Session and DeepSeek, shared cap 80.
Run explicitly with --live; never sends grading notes or generates profiles.
"""
import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config, session as session_module
from backend.llm import ModelClient, ModelError
from backend.schemas import Lesson

SELECTION = {'01':[1,3,5,6,7], '02':[1,2,4,5,7], '03':[1,3,5,6,7],
             '04':[1,2,3,6,7], '05':[1,2,3,4,7]}
LIMIT = 80


def scenarios():
    result=[]
    for path in sorted((ROOT/'docs/学生反应回归测试').glob('0*.md')):
        number=path.name[:2]; text=path.read_text(encoding='utf-8')
        profile=re.search(r'### 固定学生画像[^\n]*\n\n```text\n(.*?)\n```',text,re.S)[1]
        lesson=Lesson(topic=re.search(r'主题：(.*?)。年级：',text)[1],
            grade=re.search(r'年级：(.*?)；',text)[1],subject=re.search(r'学科：(.*?)；',text)[1],
            points=re.search(r'知识点填写：(.*?)。',text)[1]+'。',student_profile=profile,
            student_level='normal',lesson_type='new',textbook='')
        turns=[]
        for label,heading,block in re.findall(r'### ([BC]\d+)([^\n]*)\n\n((?:>[^\n]*\n?)+)',text):
            if label.startswith('B') or int(label[1:]) in SELECTION[number]:
                teacher='\n'.join(line.removeprefix('> ').removeprefix('>') for line in block.strip().splitlines())
                turns.append({'label':label,'teacher':teacher,'required_reply':'提问' in heading})
        assert len(turns)==13, (number,len(turns))
        result.append((number,lesson,turns))
    assert len(result)==5
    return result


async def run(args):
    planned=scenarios()
    if args.dry_run:
        print(json.dumps({n:{'turns':[t['label'] for t in ts], 'profile_chars':len(l.student_profile)}
                          for n,l,ts in planned},ensure_ascii=False,indent=2));return
    if not args.live:
        raise RuntimeError('Explicit --live required')
    if not config.API_KEY or 'deepseek' not in config.BASE_URL or not config.MODEL.startswith('deepseek'):
        raise RuntimeError('Authorized DeepSeek configuration required')
    output=(ROOT/args.output).resolve();output.mkdir(parents=True,exist_ok=False)
    config.ROOT=output
    summary={'started_at':datetime.now().astimezone().isoformat(),'status':'running',
        'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'material_commit':'7597b49a28da4dcf04ac34d1399b150141a2c481',
        'model':config.MODEL,'base_url':config.BASE_URL,'request_limit':LIMIT,
        'requests':0,'responses':0,'usage':{},'phases':{},'cases':[],
        'scope':'fixed profiles; full B then selected C; 65 teacher inputs; no A; one sample',
        'input_mode':'text; unmuted; TTS off; ASR review off; serial',
        'implementation_sha256':{}}
    for name in ['backend/session.py','backend/llm.py','backend/prompts.py','backend/schemas.py','backend/policy.py','backend/cognition.py','tests/live_screenshot_rebase_regression.py']:
        raw=(ROOT/name).read_bytes();summary['implementation_sha256'][name]=hashlib.sha256(raw).hexdigest()
        (output/(name.replace('/','__')+'.snapshot')).write_bytes(raw)
    budget={'limit':LIMIT,'requests':0,'phases':{},'blocking_error':None}
    def safe_write(path,obj):
        text=json.dumps(obj,ensure_ascii=False,indent=2).replace(config.API_KEY,'[REDACTED]')
        path.write_text(text,encoding='utf-8')
    def save():
        safe_write(output/'summary.json',summary);safe_write(output/'budget.json',budget)
    active={'case':None,'turn':None}
    class Tracked(ModelClient):
        def __init__(self,emit):
            async def track(kind,data):
                if kind=='llm_request':
                    if budget['blocking_error']:
                        raise ModelError(budget['blocking_error'])
                    if budget['requests']>=LIMIT:
                        raise ModelError('Reached shared 80-request cap')
                    budget['requests']+=1;summary['requests']+=1
                    phase=data['phase'];budget['phases'][phase]=budget['phases'].get(phase,0)+1
                    summary['phases']=dict(budget['phases']);save()
                if kind=='llm_output':
                    summary['responses']+=1
                    for k,v in data.get('usage',{}).items():
                        if isinstance(v,(int,float)):
                            summary['usage'][k]=summary['usage'].get(k,0)+v
                    save()
                event={'case':active['case'],'turn':active['turn'],'event':kind,'data':data}
                with (output/'events.jsonl').open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps(event,ensure_ascii=False).replace(config.API_KEY,'[REDACTED]')+'\n')
                await emit(kind,data)
            super().__init__(track)
        async def generate(self,*a,**kw):
            try:return await super().generate(*a,**kw)
            except ModelError as exc:
                if re.search(r'HTTP (401|402|403)\b',str(exc)):
                    budget['blocking_error']=str(exc);save()
                raise
    session_module.ModelClient=Tracked
    save()
    try:
        for number,lesson,turns in planned:
            case={'id':number,'settings':lesson.model_dump(),'planned_turns':[t['label'] for t in turns],
                  'status':'running','turns':[],'issues':[]}
            summary['cases'].append(case);active.update(case=number,turn=None)
            if budget['blocking_error'] or budget['requests']>=LIMIT:
                case.update(status='not_run',error=budget['blocking_error'] or 'Shared cap reached');save();continue
            events=[]
            async def send(event):events.append(event)
            session=session_module.Session(send,None);case['session_id']=session.id
            try:
                await session.start(lesson,muted=False,tts=False,asr_review=False)
                case['preparation']=session.preparation.model_dump();case['initial_state']=session.state.model_dump()
                for turn in turns:
                    active['turn']=turn['label'];record={**turn,'status':'running','started_at':datetime.now().astimezone().isoformat()}
                    case['turns'].append(record);save();before=len(events);started=time.monotonic()
                    await session.add_transcript(turn['teacher']);await session.flush()
                    async with asyncio.timeout(240):
                        while True:
                            if session.failed_batch:
                                raise ModelError(next(e['data']['message'] for e in reversed(events) if e['type']=='error'))
                            if any(e['type']=='state' for e in events[before:]) and not session.processing and session.teaching_queue.empty():
                                break
                            await asyncio.sleep(.05)
                        # Let the production review task consume its wake signal;
                        # never suppress background review merely to save requests.
                        await asyncio.sleep(.1)
                        while session.knowledge_review_busy or any(r.status=='pending' for r in session.state.pending_reviews):
                            await asyncio.sleep(.05)
                    await session.try_speak(60)
                    local=events[before:]
                    record.update(status='completed',seconds=round(time.monotonic()-started,2),
                        delivered_replies=[e['data'] for e in local if e['type']=='reply'],
                        state=session.state.model_dump(),
                        candidates=[e['data']['candidate'] for e in local if e['type']=='state'],
                        source_ids=[e['data']['id'] for e in local if e['type']=='transcript'],
                        diagnostics=[e for e in local if e['type'] in ('error','student_recovery','suppressed','knowledge_review')])
                    record['delivery']='passed' if not turn['required_reply'] or record['delivered_replies'] else 'failed'
                    if record['delivery']=='failed':case['issues'].append(turn['label']+': explicit question had no delivered reply')
                    if any(r.status=='failed' for r in session.state.pending_reviews):case['issues'].append(turn['label']+': knowledge review failed')
                    print(number,turn['label'],record['delivery'],'requests',budget['requests'],flush=True);save()
                await session.finish();case['status']='completed'
            except Exception as exc:
                case.update(status='failed',error=str(exc))
                if case['turns'] and case['turns'][-1]['status']=='running':case['turns'][-1].update(status='failed',error=str(exc))
                print(number,'FAILED',str(exc),'requests',budget['requests'],flush=True)
            finally:
                await session.close();case['final_state']=session.state.model_dump();save()
        summary['status']='completed' if all(c['status']=='completed' for c in summary['cases']) else 'incomplete'
    finally:
        summary['finished_at']=datetime.now().astimezone().isoformat();save()
    print('RESULT',summary['status'],summary['requests'],summary['usage'],str(output),flush=True)
    if summary['status']!='completed':raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true');parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--output',default='logs/screenshot-rebase-live-20261016')
    asyncio.run(run(parser.parse_args()))
