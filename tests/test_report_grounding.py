import asyncio
import json
from pathlib import Path
import runpy

import pytest

from backend import report_contract as contract

fixture = runpy.run_path(str(Path(__file__).with_name('test_teaching_report.py')))


def classroom_turns():
    return [
        {'id':'t1','role':'teacher','text':'请看图，写出吸热公式。'},
        {'id':'r1','role':'ai','kind':'answer','text':'Q=cmΔt。'},
        {'id':'t2','role':'teacher','text':'请写出图中数据、单位换算和代入式。'},
        {'id':'r2','role':'ai','kind':'answer','text':'500g换为0.5kg，Q=4200×0.5×20=42000J。'},
    ]


def output():
    obj = fixture['model_output']()
    def rewrite(value):
        if isinstance(value, dict):
            if 'source_ids' in value: value['source_ids'] = ['t1', 't2']
            for child in value.values(): rewrite(child)
        elif isinstance(value, list):
            for child in value: rewrite(child)
    rewrite(obj)
    return obj


def test_cannot_deny_delivered_ai_responses():
    obj = output()
    obj['dimensions']['checking']['reason'] = '仅见教师布置任务，无学生回应或教师反馈材料'
    with pytest.raises(ValueError, match='实际AI回应'):
        contract.validate_grounding(contract.TeachingReport.model_validate(obj), classroom_turns())


@pytest.mark.parametrize('reason', ['有AI回答，但没有教师后续反馈', '没有真实学生回应，不能证明真实学生掌握'])
def test_no_teacher_feedback_and_no_real_student_claims_are_allowed(reason):
    obj = output(); obj['dimensions']['checking']['reason'] = reason
    contract.validate_grounding(contract.TeachingReport.model_validate(obj), classroom_turns())


def test_question_does_not_count_as_answer():
    obj = output(); obj['dimensions']['checking']['reason'] = '没有学生回答，只有AI提出疑惑'
    turns = [t for t in classroom_turns() if t['role']=='teacher']
    turns.append({'id':'r1','role':'ai','kind':'question','text':'如何计算？'})
    contract.validate_grounding(contract.TeachingReport.model_validate(obj), turns)


def test_teacher_demo_needs_teacher_evidence_not_ai_calculation():
    obj = output(); obj['keep']['text'] = '教师示范换算和代入计算'
    with pytest.raises(ValueError, match='示范'):
        contract.validate_grounding(contract.TeachingReport.model_validate(obj), classroom_turns())


def test_teacher_demo_rejects_borrowed_ai_quote_and_instruction():
    obj = output(); obj['keep']['text'] = '教师示范换算和代入计算'
    for evidence in [
        {'actor':'teacher','action':'demonstration','source_id':'r2','quote':'500g换为0.5kg'},
        {'actor':'teacher','action':'demonstration','source_id':'t2','quote':'请写出图中数据、单位换算和代入式。'},
    ]:
        obj['action_evidence'] = [evidence]
        with pytest.raises(ValueError):
            contract.validate_grounding(contract.TeachingReport.model_validate(obj), classroom_turns())


def test_actual_teacher_demo_is_supported_and_future_practice_is_not_past_action():
    obj = output(); obj['keep']['text'] = '保留教师示范换算的做法'
    quote = '先把500g换成0.5kg，再代入4200×0.5×20，结果42000J。'
    obj['action_evidence'] = [{'actor':'teacher','action':'demonstration','source_id':'t2','quote':quote}]
    turns = classroom_turns(); turns[2]['text'] = quote
    contract.validate_grounding(contract.TeachingReport.model_validate(obj), turns)


def test_structured_classroom_facts_exclude_unissued_candidates():
    from backend.report_pipeline import compact
    turns = classroom_turns()
    obj = compact({'lesson':{'topic':'比热容'},'turns':turns})
    assert obj['interaction_facts']['delivered_ai_reply_ids'] == ['r1','r2']
    assert obj['interaction_facts']['teacher_after_last_reply_ids'] == []


def test_wrong_role_claim_is_repaired_within_existing_two_requests(monkeypatch):
    import httpx
    from backend import config
    from backend.llm import ModelClient
    monkeypatch.setattr(config, 'API_KEY', 'fake-key')
    async def run():
        calls=[]
        async def emit(*args): pass
        def respond(request):
            calls.append(json.loads(request.content))
            obj=output()
            if len(calls)==1:
                obj['keep']['text']='教师示范换算和代入计算'
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(obj)}}]})
        model=ModelClient(emit)
        await model.http.aclose()
        model.http=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            bound=contract.source_bound(contract.TeachingReport, {'t1','t2'}, classroom_turns())
            result=await model.generate('teaching_report','',{},bound)
            assert len(calls)==2
            assert '示范' not in result.keep.text
            assert 'action_evidence' in calls[1]['messages'][-1]['content']
        finally: await model.close()
    asyncio.run(run())


def test_reading_invalid_grounding_cache_never_calls_model(tmp_path, monkeypatch):
    from backend import config
    from backend.teaching_report import ReportService, read_classroom, source_hash
    path=fixture['save_classroom'](tmp_path)
    classroom=read_classroom(tmp_path,fixture['SID'])
    monkeypatch.setattr(config,'ROOT',tmp_path)
    service=ReportService(lambda emit: pytest.fail('read must not instantiate a model'))
    obj=fixture['model_output']()
    obj['dimensions']['checking']['reason']='没有学生回应'
    saved={'schema_version':2,'session_id':fixture['SID'],'source_hash':source_hash(classroom),
           'status':'ready','progress':'完成','report':obj,'error':''}
    service.save(saved)
    original=service.path(fixture['SID']).read_bytes()
    assert service.get(fixture['SID'])['status']=='failed'
    assert service.path(fixture['SID']).read_bytes()==original


def test_advice_about_existing_demonstration_is_not_a_new_teacher_claim():
    obj=output()
    obj['finding']['text']='可让学生自己列式，避免直接套用示范步骤。'
    obj['finding']['source_ids']=['t1']
    quote='先把500g换成0.5kg，再代入4200×0.5×20，结果42000J。'
    obj['action_evidence']=[{'actor':'teacher','action':'demonstration','source_id':'t2','quote':quote}]
    turns=classroom_turns();turns[2]['text']=quote
    contract.validate_grounding(contract.TeachingReport.model_validate(obj),turns)


@pytest.mark.parametrize('reason', [
    '学生两次追问被开方数为0，教师未回应即结束',
    '学生两次追问被开方数为0，教师均未澄清',
    '学生两次追问被开方数为0，教师未澄清该具体疑问',
    '对被开方数为0的追问没有给出反馈',
])
def test_concrete_unresolved_question_does_not_require_magic_word(reason):
    obj = output()
    obj['dimensions']['checking'].update(label='可改进', reason=reason)
    turns = [
        {'id':'t1','role':'teacher','text':'√(x－1)需要x＞1，0没有意义。'},
        {'id':'r1','role':'ai','kind':'question','text':'为什么x＝1时√0没有意义？'},
        {'id':'t2','role':'teacher','text':'这个先不解释，下课。'},
    ]
    contract.validate_grounding(contract.TeachingReport.model_validate(obj), turns)


def test_completed_reason_check_is_factual_not_a_mastery_claim():
    turns=[
        {'id':'t1','role':'teacher','text':'请说明换算依据。'},
        {'id':'r1','role':'ai','kind':'answer','text':'因为单位要对应。'},
        {'id':'t2','role':'teacher','text':'这个换算理由正确。'},
    ]
    assert contract.interaction_facts(turns)['completed_reason_checks']==[
        {'teacher_question_id':'t1','ai_answer_id':'r1','teacher_feedback_id':'t2'}]


def test_coordinate_reading_has_explicit_precision_rule():
    from backend.prompts import STUDENT
    assert '坐标' in STUDENT and '不能随意取整' in STUDENT


def test_explicit_teacher_worked_result_does_not_require_model_to_repeat_metadata():
    obj=output();obj['overview']['text']='教师演示换算与代入计算'
    turns=classroom_turns()
    turns[2]['text']='看这个例子：先把500g换成0.5kg，再代入4200×0.5×20，结果42000J。'
    contract.validate_grounding(contract.TeachingReport.model_validate(obj),turns)


def test_request_with_an_expression_is_not_a_worked_demonstration():
    obj=output();obj['overview']['text']='教师演示换算与代入计算'
    turns=classroom_turns()
    turns[2]['text']='请用4200×0.5×20计算，结果是多少？'
    with pytest.raises(ValueError,match='示范'):
        contract.validate_grounding(contract.TeachingReport.model_validate(obj),turns)


@pytest.mark.parametrize('feedback',['这个理由不正确。','对，不过你给出的依据有误。'])
def test_negative_feedback_is_not_a_successful_reason_check(feedback):
    turns=[{'id':'t1','role':'teacher','text':'请说明依据。'},
           {'id':'r1','role':'ai','kind':'answer','text':'因为质量可以不用换算。'},
           {'id':'t2','role':'teacher','text':feedback}]
    assert contract.interaction_facts(turns)['completed_reason_checks']==[]


def test_calibration_metadata_does_not_grow_with_unanswered_monologue():
    turns=[{'id':'r1','role':'ai','kind':'answer','text':'回答。'}]
    turns += [{'id':f't{i}','role':'teacher','text':'继续解释。'} for i in range(30)]
    assert len(contract.interaction_facts(turns)['teacher_after_last_reply_ids'])<=8
