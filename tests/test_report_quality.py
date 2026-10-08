"""教师阅读质量回归，不调用外部模型。"""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from backend import config
from backend.llm import ModelClient, ModelError
from backend.report_models import Diagnosis
from backend.report_v2 import validate_diagnosis, markdown
from tests.test_report_v2 import SID, classroom
from backend.report_data import build_snapshot


def teacher_diagnosis(snapshot):
    source = snapshot['learning_sources'][0]
    cite = {'event_id': source['event_id'], 'quote': source['text']}
    return {'summary': {'text': '讲授说明了节点组成，但未用作答检查这条规则的应用。', 'citations':[cite]},
        'strengths': [{'id':'f1','observation':'讲授给出了节点组成。','interpretation':'可保留明确拆分结构的讲法。','citations':[cite]}],
        'weaknesses':[{'id':'f2','basis':'教学内容或检查机会','observation':'讲授直接给出节点组成，课堂没有学生作答。',
            'interpretation':'目前无法知道学生能否自行应用，应安排检查。','citations':[cite]}],
        'suggestions':[{'finding_id':'f2','priority':'优先','action':'给一个新的节点图，让学生指出数据和后继引用并解释依据。'}]}


def test_teacher_only_can_receive_evidenced_teaching_feedback():
    snapshot = build_snapshot(SID, classroom())
    diagnosis = teacher_diagnosis(snapshot)
    validate_diagnosis(diagnosis, snapshot)
    assert len(diagnosis['weaknesses']) == len(diagnosis['suggestions']) == 1


def test_teacher_only_cannot_invent_student_performance():
    snapshot = build_snapshot(SID, classroom())
    diagnosis = teacher_diagnosis(snapshot)
    diagnosis['weaknesses'][0]['observation'] = '学生回答错误，说明学生没有理解。'
    with pytest.raises(ValueError, match='学生'):
        validate_diagnosis(diagnosis, snapshot)


def test_invalid_structure_exposes_field_and_retry_feedback(monkeypatch):
    monkeypatch.setattr(config, 'API_KEY', 'test-secret-not-a-key')
    events, bodies = [], []
    async def emit(kind, data): events.append((kind, deepcopy(data)))
    def response(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps({'strengths':[{'id':'f9'}]})}}]})
    async def run():
        client = ModelClient(emit)
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(response))
        try:
            with pytest.raises(ModelError, match='strengths.0.id'):
                await client.generate('report_v2_diagnosis', 'JSON', {}, Diagnosis)
        finally: await client.close()
    asyncio.run(run())
    assert len(bodies) == 2
    assert 'strengths.0.id' in bodies[1]['messages'][-1]['content']
    assert any(kind == 'llm_validation_error' for kind, _ in events)
    assert config.API_KEY not in json.dumps(events)


def test_failed_diagnosis_is_not_reported_as_no_teaching_problem():
    snapshot = build_snapshot(SID, classroom())
    view = {'snapshot':snapshot,'status':'partial','diagnosis':None,'recall':None,'answers':None,'verification':None,
        'stages':{'diagnosis':{'status':'failed','error':'模型返回格式异常'}}}
    text = markdown(view)
    assert '教学分析生成失败' in text and '模型返回格式异常' in text
    assert '暂无有依据的建议' not in text


def test_teacher_feedback_precedes_collapsed_data_and_stats():
    snapshot = build_snapshot(SID, classroom())
    view = {'snapshot':snapshot,'status':'ready','diagnosis':teacher_diagnosis(snapshot),'recall':None,
        'answers':None,'verification':None,'stages':{}}
    text = markdown(view)
    assert text.index('课堂总体评价') < text.index('本次试讲结算')
    assert '<details>' in text and text.index('<details>') < text.index('本次试讲结算')
    assert '没有课堂学生发言' in text


@pytest.mark.parametrize('kind,expected', [('仅表面替换','尚未验证'),('无法判断','证据不足')])
def test_cosmetic_example_and_unknown_comparison_are_not_transfer(kind, expected):
    from tests.test_report_v2_acceptance import verification_case, snapshot_for
    from backend.report_v2 import validate_verification
    answer='这个说法不成立，前一步重新赋值允许，后一步改内部元素不允许，因为操作的对象不同。'
    plan, answers, result = verification_case(answer)
    result['results'][0]['reasoning_check']['comparison']['kind']=kind
    validate_verification(result,plan,answers,snapshot_for())
    assert result['results'][0]['status']==expected


def test_wrong_comparison_quote_cannot_prove_transfer():
    from tests.test_report_v2_acceptance import verification_case, snapshot_for
    from backend.report_v2 import validate_verification
    plan, answers, result=verification_case('先赋值允许，后改元素不允许，因为两步操作对象不同。')
    result['results'][0]['reasoning_check']['comparison']['answer_quote']='答案中不存在的迁移过程'
    validate_verification(result,plan,answers,snapshot_for())
    assert result['results'][0]['status']=='证据不足'


def test_real_cached_surface_examples_are_conservative_without_new_requests():
    from pathlib import Path
    from backend.report_v2 import ReportService
    source=Path(__file__).resolve().parents[1]/'logs'/'99035dfce2f8.report-v2.json'
    if not source.exists(): pytest.skip('隐私课堂缓存不入仓库；公开用例独立覆盖相同约束')
    service=ReportService(); service.views['99035dfce2f8']=json.loads(source.read_text(encoding='utf-8'))
    view=service.get('99035dfce2f8')
    assert len(view['verification']['results'])==3
    assert all(v['status'] in ('证据不足','尚未验证') for v in view['verification']['results'])
    assert view['snapshot']['settlement']['metrics']['student_replies']['value']==0


def test_failed_phase_retry_preserves_success_and_persisted_feedback(tmp_path,monkeypatch):
    from tests.test_report_v2 import service_for
    from backend.report_v2 import ReportService
    service,calls=service_for(tmp_path,monkeypatch,fail='report_v2_diagnosis')
    class Recover:
        async def generate(self,phase,system,payload,schema):
            calls.append((phase,payload))
            return schema.model_validate(teacher_diagnosis(service.get(SID)['snapshot']))
        async def close(self): pass
    async def run():
        service.start(SID);await service.tasks[SID]
        service.client_factory=lambda emit:Recover()
        service.start(SID);await service.tasks[SID]
    asyncio.run(run())
    restored=ReportService().get(SID)
    assert restored['status']=='ready'
    assert len(restored['diagnosis']['weaknesses'])==1
    assert [p for p,_ in calls].count('report_v2_recall')==1
    assert [p for p,_ in calls].count('report_v2_diagnosis')==2


@pytest.mark.parametrize('error', ['模型请求超时，请重试。','模型服务返回 HTTP 401，请检查密钥、额度或模型配置'])
def test_failure_reason_retained_in_reader_and_markdown(tmp_path,monkeypatch,error):
    from tests.test_report_v2 import service_for
    service,_=service_for(tmp_path,monkeypatch)
    service.views[SID]['status']='partial'
    service.views[SID]['stages']['diagnosis']={'status':'failed','error':error}
    view=service.get(SID)
    assert view['reader']['diagnosis_failed'] and view['reader']['error']==error
    assert error in markdown(view)


@pytest.mark.parametrize('failure', ['timeout','nonjson','http'])
def test_transport_and_json_failures_are_bounded_and_not_success(monkeypatch,failure):
    monkeypatch.setattr(config,'API_KEY','test-only-key')
    calls=[]
    async def emit(kind,data): pass
    def handler(request):
        calls.append(request)
        if failure=='timeout': raise httpx.ReadTimeout('slow',request=request)
        if failure=='http': return httpx.Response(429,json={'error':'quota'})
        return httpx.Response(200,json={'choices':[{'message':{'content':'{ broken'}}]})
    async def run():
        client=ModelClient(emit); await client.http.aclose()
        client.http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ModelError): await client.generate('report_v2_diagnosis','JSON',{},Diagnosis)
        finally: await client.close()
    asyncio.run(run())
    assert len(calls)==(2 if failure=='nonjson' else 1)


def test_stats_distinguish_zero_unannotated_missing_and_no_interaction():
    from backend.report_reader import reader_view
    snapshot=build_snapshot(SID,classroom())
    snapshot['settlement']['metrics']['teacher_questions']['value']=0
    view={'snapshot':snapshot,'diagnosis':None}
    reader=reader_view(view)
    assert reader['metrics']['teacher_questions']['state']=='observed'
    assert reader['metrics']['teacher_questions']['display']=='0'
    assert reader['metrics']['student_replies']['state']=='not_occurred'
    assert reader['metrics']['verified_understanding']['state']=='not_measured'
    snapshot['settlement']['metrics']['teacher_questions']['value']=None
    assert reader_view(view)['metrics']['teacher_questions']['state']=='unknown'
    del snapshot['settlement']['metrics']['duration_seconds']['value']
    assert reader_view(view)['metrics']['duration_seconds']['state']=='missing'


def test_followup_citations_do_not_expand_saved_schema_and_are_visible():
    from backend.report_reader import reader_view
    snapshot=build_snapshot(SID,classroom())
    diagnosis=teacher_diagnosis(snapshot)
    diagnosis['weaknesses'][0]['followup_citations']=deepcopy(diagnosis['weaknesses'][0]['citations'])
    raw=deepcopy(diagnosis)
    validate_diagnosis(diagnosis,snapshot)
    assert diagnosis==raw
    Diagnosis.model_validate(diagnosis)
    assert reader_view({'snapshot':snapshot,'diagnosis':diagnosis})['issues'][0]['citations']


@pytest.mark.parametrize('steps',[['定位元素'],['选择容器'],['计算一次']])
def test_model_cannot_label_identical_demonstration_steps_as_transfer(steps):
    from tests.test_report_v2_acceptance import verification_case, snapshot_for
    from backend.report_v2 import validate_verification
    plan, answers, result=verification_case('我能按题目要求得出正确答案并说明原因。')
    result['results'][0]['reasoning_check']['comparison'].update(kind='实质任务变化',teacher_steps=steps,task_steps=steps)
    validate_verification(result,plan,answers,snapshot_for())
    assert result['results'][0]['status']=='尚未验证'


def test_empty_diagnosis_object_is_not_a_success_but_old_reports_remain_readable():
    snapshot=build_snapshot(SID,classroom())
    empty=Diagnosis.model_validate({}).model_dump()
    with pytest.raises(ValueError,match='空对象'):
        validate_diagnosis(empty,snapshot,require_current=True)
    validate_diagnosis(empty,snapshot)  # Compatibility projection never spends API credits.


def test_wrong_answer_followed_by_correction_is_not_persistent_teaching_failure():
    from tests.test_report_v2_acceptance import question_classroom, SID as QUESTION_SID, TEACHER
    events=question_classroom()
    wrong='元组不可变，所以重新赋值和修改元素都不允许。'
    events.insert(3,{'id':QUESTION_SID+':e000010','type':'reply','elapsed':2.5,
                     'data':{'kind':'answer','question_id':'q1','text':wrong}})
    snapshot=build_snapshot(QUESTION_SID,events)
    citations=[{'event_id':QUESTION_SID+':e000010','quote':wrong},
               {'event_id':QUESTION_SID+':e000004','quote':TEACHER}]
    finding={'id':'f1','observation':'学生回答把重新赋值和修改元素混为一谈。','interpretation':'这一错误说明教师教学失败。','citations':citations}
    diagnosis={'strengths':[{'id':'f2','observation':'教师在错误回答后区分重新赋值和修改元素。','interpretation':'纠正针对学生说出的错误。','citations':citations}],
               'weaknesses':[finding],'suggestions':[]}
    validate_diagnosis(diagnosis,snapshot)
    assert diagnosis['weaknesses']==[] and len(diagnosis['strengths'])==1


def test_markdown_teacher_evidence_links_have_actual_targets():
    import re
    snapshot=build_snapshot(SID,classroom())
    view={'snapshot':snapshot,'diagnosis':teacher_diagnosis(snapshot),'status':'ready','stages':{}}
    text=markdown(view)
    links=re.findall(r'\]\(#(report-evidence-[^)]+)\)',text.split('<details>')[0])
    assert links and all(f'id="{anchor}"' in text for anchor in links)
    assert '"knowledge"' not in text  # Repeated state JSON stays in the source cache and detailed web view.
