import asyncio
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from backend import report, config
from backend.app import app
from backend.schemas import TrialReportAnalysis


SID = '123456abcdef'
RULE = '星星运算的完整规则是两数相加以后再减1。'


@pytest.fixture
def classroom(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    (tmp_path/'logs').mkdir()
    state = {'version': 2, 'knowledge': [
        {'id':'old','text':'两数直接相加','status':'conflict','sources':['t0']},
        {'id':'new','text':RULE,'status':'understood','sources':['t1']}],
        'open_questions': [], 'recent_events':[{'id':'end','kind':'lesson_end','text':'今天学习结束','sources':['t2']}],
        'knowledge_history':[]}
    records = [
        {'type':'transcript','data':{'id':'t0','text':'两数直接相加'}},
        {'type':'transcript','data':{'id':'t1','text':RULE}},
        {'type':'transcript','data':{'id':'t2','text':'今天学习结束'}},
        {'type':'reply','data':{'text':'6★2=7','sources':['t1']}},
        {'type':'finished','data':{'session_id':SID,'state':state,'assessments':[],'unprocessed_sources':[]}}]
    path = tmp_path/'logs'/f'{SID}.jsonl'
    def save():
        path.write_text('\n'.join(json.dumps(e,ensure_ascii=False) for e in records),encoding='utf-8')
    save()
    return path, records, save


def analysis(**changes):
    value = {'taught_points':[{'observation':'已讲授星星运算规则', 'interpretation':'当前规则包含减1',
        'basis':'classroom','knowledge_ids':['new'],'citations':[{'source_id':'t1','quote':RULE}]}]}
    value.update(changes)
    return TrialReportAnalysis.model_validate(value)


class FakeClient:
    calls = 0
    outputs = None
    payload = None
    closed = False
    def __init__(self, emit): pass
    async def generate(self, phase, system, payload, output):
        FakeClient.calls += 1
        FakeClient.payload = deepcopy(payload)
        assert phase == 'trial_report'
        return FakeClient.outputs or analysis()
    async def close(self): FakeClient.closed = True


@pytest.fixture
def model(monkeypatch):
    FakeClient.calls=0; FakeClient.outputs=None; FakeClient.closed=False
    monkeypatch.setattr(report,'ModelClient',FakeClient)
    return FakeClient


def test_report_is_evidenced_cached_and_does_not_change_classroom(classroom, model):
    path,_,_=classroom;original=path.read_bytes()
    result=asyncio.run(report.generate_report(SID))
    assert result['facts']['assessment_count']==0
    assert any('未做独立测验' in note for note in result['limitations'])
    assert 'verified' not in result['analysis']['taught_points'][0]
    assert model.payload['current_state']['knowledge'][0]['id']=='new'
    assert model.payload['current_state']['inactive_knowledge'][0]['id']=='old'
    assert '# 课后试讲反馈报告' in result['markdown']
    assert result['analysis']['taught_points'][0]['citations'][0]['quote']==RULE
    again=asyncio.run(report.generate_report(SID))
    assert again['cached'] and model.calls==1 and model.closed
    assert path.read_bytes()==original
    assert (path.parent/f'{SID}.report.json').exists()


@pytest.mark.parametrize('failure',['wrong_quote','unknown_source','inactive','event_as_knowledge','fake_assessment','student_as_teaching'])
def test_unsupported_conclusions_are_not_saved(classroom,model,failure):
    item=analysis().model_dump()['taught_points'][0]
    if failure=='wrong_quote':item['citations'][0]['quote']='不存在的原文'
    if failure=='unknown_source':item['citations'][0]['source_id']='t100'
    if failure=='inactive':item['knowledge_ids']=['old']
    if failure=='event_as_knowledge':item['knowledge_ids']=[];item['citations']=[{'source_id':'t2','quote':'今天学习结束'}]
    if failure=='fake_assessment':item['basis']='assessment'
    if failure=='student_as_teaching':item['citations'].append({'source_id':'r1','quote':'6★2=7'})
    model.outputs=analysis(taught_points=[item])
    with pytest.raises(ValueError):asyncio.run(report.generate_report(SID))
    assert not (classroom[0].parent/f'{SID}.report.json').exists()


def test_incomplete_records_are_disclosed_and_not_counted_as_learned(classroom,model):
    _,records,save=classroom;records[-1]['data']['unprocessed_sources']=['t2'];save()
    result=asyncio.run(report.generate_report(SID))
    assert result['facts']['unprocessed_sources']==['t2']
    assert '报告不完整' in result['limitations'][0]


def test_assessment_evidence_is_preserved_without_promoting_knowledge(classroom,model):
    path,records,save=classroom
    records[-1]['data']['assessments']=[{'id':'a1','question':{'text':'6★2怎么算'},'answer':{'text':'6+2-1=7'},
        'evaluation':{'verdict':'passed','coverage':'sufficient','explanation':'按新规则应用'}}];save()
    item=analysis().model_dump()['taught_points'][0]
    item.update(basis='assessment',citations=[{'source_id':'t1','quote':RULE},{'source_id':'a1','quote':'6+2-1=7'}])
    model.outputs=analysis(understanding=[item])
    original=path.read_bytes();result=asyncio.run(report.generate_report(SID))
    assert result['facts']['assessment_verdicts']=={'passed':1}
    assert path.read_bytes()==original


def test_changed_log_invalidates_cache(classroom,model):
    asyncio.run(report.generate_report(SID))
    _,records,save=classroom;records[-1]['data']['state']['version']=3;save()
    assert not asyncio.run(report.generate_report(SID))['cached']
    assert model.calls==2


def test_changed_report_version_invalidates_cache(classroom,model):
    asyncio.run(report.generate_report(SID))
    path=classroom[0].parent/f'{SID}.report.json'
    cached=json.loads(path.read_text(encoding='utf-8'));cached['format_version']=0
    path.write_text(json.dumps(cached),encoding='utf-8')
    assert not asyncio.run(report.generate_report(SID))['cached']
    assert model.calls==2


def test_quote_failure_gets_one_corrective_retry_without_relaxing_validation(classroom,model,monkeypatch):
    original=FakeClient.generate
    async def repair(self,phase,system,payload,output):
        value=await original(self,phase,system,payload,output)
        if model.calls==1:
            value=value.model_copy(deep=True)
            value.taught_points[0].citations[0].quote='模型改写过的引文'
        else:
            assert 't1' in payload['validation_feedback']
            assert payload['previous_report']['taught_points'][0]['citations'][0]['quote']=='模型改写过的引文'
        return value
    monkeypatch.setattr(FakeClient,'generate',repair)
    result=asyncio.run(report.generate_report(SID))
    assert model.calls==2 and model.closed
    assert result['analysis']['taught_points'][0]['citations'][0]['quote']==RULE


def test_unfinished_classroom_cannot_generate(classroom,model):
    _,records,save=classroom;records.pop();save()
    with pytest.raises(ValueError,match='先结束'):asyncio.run(report.generate_report(SID))
    assert model.calls==0


def test_endpoint_validation_and_local_origin(classroom,model):
    with TestClient(app) as client:
        assert client.post(f'/api/sessions/{SID}/report').status_code==200
        assert client.post('/api/sessions/not-valid/report').status_code==400
        assert client.post('/api/sessions/aaaaaaaaaaaa/report').status_code==404
        assert client.post(f'/api/sessions/{SID}/report',headers={'Origin':'https://other.example'}).status_code==403


def test_failed_generation_can_retry_and_closes_client(classroom,model,monkeypatch):
    from backend.llm import ModelError
    async def fail(*args): raise ModelError('模拟服务失败')
    with monkeypatch.context() as patch:
        patch.setattr(FakeClient,'generate',fail)
        with pytest.raises(ModelError):asyncio.run(report.generate_report(SID))
    assert model.closed and not (classroom[0].parent/f'{SID}.report.json').exists()
    assert asyncio.run(report.generate_report(SID))['analysis']


def test_oversize_context_is_not_silently_truncated(classroom,model):
    _,records,save=classroom;records[0]['data']['text']='教'*160001;save()
    with pytest.raises(ValueError,match='过长'):asyncio.run(report.generate_report(SID))
    assert model.calls==0


def test_duplicate_requests_only_generate_once(classroom,model,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    original=FakeClient.generate
    async def slow(self,*args):
        await asyncio.sleep(.08)
        return await original(self,*args)
    monkeypatch.setattr(FakeClient,'generate',slow)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as workers:
        results=list(workers.map(lambda _:client.post(f'/api/sessions/{SID}/report'),range(2)))
    assert all(r.status_code==200 for r in results)
    assert model.calls==1


def test_teacher_changes_cannot_imply_student_old_belief(classroom):
    context,_,_=report.load_classroom(SID)
    item={'observation':'学生最初可能认为两数直接相加，后来修正。','interpretation':'旧认知已改变。',
          'basis':'classroom','citations':[{'source_id':'t0','quote':'两数直接相加'}, {'source_id':'t1','quote':RULE}]}
    with pytest.raises(ValueError,match='学生回答证据'):
        report.validate_analysis(analysis(changes=[item]),context)
    item.update(observation='教师最初说两数直接相加，随后补充减1。',interpretation='教师修订了讲授规则。')
    report.validate_analysis(analysis(changes=[item]),context)


def test_later_assessment_evaluation_is_not_proof_of_earlier_student_error(classroom):
    _,records,save=classroom
    records[-1]['data']['assessments']=[{'id':'a1','question':{'text':'小明只相加对吗？'},
        'answer':{'text':'不对，应该再减1。'},'evaluation':{'verdict':'passed','coverage':'sufficient','explanation':'学生正确应用新规则'}}];save()
    context,_,_=report.load_classroom(SID)
    item={'observation':'学生最初可能认为只相加，后来修正。','interpretation':'旧认知被覆盖。',
          'basis':'assessment','citations':[{'source_id':'a1','quote':'学生正确应用新规则'}]}
    with pytest.raises(ValueError,match='学生回答证据'):
        report.validate_analysis(analysis(changes=[item]),context)


def test_real_student_error_and_repair_remain_reportable(classroom):
    _,records,save=classroom
    records.insert(-1,{'type':'reply','data':{'text':'12是偶数，蓝盒12等于27。','sources':['t1']}})
    records.insert(-1,{'type':'reply','data':{'text':'12进入特殊规则范围，规则还未教授，不能确定。','sources':['t1']}});save()
    context,_,_=report.load_classroom(SID)
    item={'observation':'学生曾把旧规则外推到12，随后修正。','interpretation':'学生随后指出特殊规则未教授，不能确定。',
          'basis':'classroom','citations':[{'source_id':'r2','quote':'蓝盒12等于27。'},
                                         {'source_id':'r3','quote':'规则还未教授，不能确定。'}]}
    report.validate_analysis(analysis(changes=[item]),context)


@pytest.mark.parametrize('claim',['可认为已掌握该规则','已经完全掌握','已牢固掌握','已证明掌握','掌握良好'])
def test_mastery_claims_rejected_without_touching_quoted_evidence(classroom,claim):
    context,_,_=report.load_classroom(SID)
    item=analysis().model_dump()['taught_points'][0]
    item['interpretation']=claim
    with pytest.raises(ValueError,match='过度掌握'):
        report.validate_analysis(analysis(understanding=[item]),context)


def test_report_prompt_requires_before_and_after_student_evidence():
    assert '后一次测验 passed 不能反向证明学生先前答错' in report.PROMPT
    assert '认知变化必须有学生侧证据' in report.PROMPT
    assert '独立测验进一步支持学生能够应用该规则' in report.PROMPT


def test_mastery_words_in_classroom_quotes_are_preserved(classroom):
    _,records,save=classroom
    records.insert(-1,{'type':'transcript','data':{'id':'t3','text':'小明说我已经完全掌握，判断这句话。'}});save()
    context,_,_=report.load_classroom(SID)
    item={'observation':'教师提出待判断的说法。','interpretation':'这句话属于题目，不证明理解程度。',
          'basis':'classroom','citations':[{'source_id':'t3','quote':'我已经完全掌握'}]}
    report.validate_analysis(analysis(understanding=[item]),context)
