"""Dense real-class failure regressions, isolated from student/state-machine code."""
import asyncio
from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend import report_pipeline as pipeline
from backend.report_contract import AnalysisNotes, TeachingReport, source_bound
from test_teaching_report import model_output


def dense_turns(count=40):
    turns=[]
    for i in range(1,count+1):
        turns.extend([{'id':f't{i}','role':'teacher','text':f'第{i}步：请说明读图的判断依据。'},
                      {'id':f'r{i}','role':'ai','kind':'answer','text':f'因为第{i}项条件如此。'}])
    return turns


def test_dense_chunks_preserve_all_turns_without_overloading_notes():
    turns=dense_turns()
    parts=list(pipeline.chunks({'lesson':{'topic':'读图'},'turns':turns}))
    assert len(parts)>1, '40组短问答不能仍挤进同一个最多20条笔记的阶段'
    assert all(sum(t['role']=='teacher' and not t.get('context_only') for t in p['turns'])<=8 for p in parts)
    original=[t for p in parts for t in p['turns'] if not t.get('context_only')]
    assert original==turns


def test_notes_receive_teacher_anchors_instead_of_citable_ai_ids(tmp_path):
    turns=dense_turns(2)
    payload={'lesson':{'topic':'读图'},'turns':turns}
    previous=deepcopy(payload)
    class Model:
        async def generate(self, phase, prompt, actual, schema):
            assert actual['allowed_teacher_source_ids']==['t1','t2']
            assert [t['teacher_source_ids'] for t in actual['turns'] if t['role']=='ai']==[['t1'],['t2']]
            assert all('id' not in t for t in actual['turns'] if t['role']=='ai')
            return schema.model_validate({'notes':[{'kind':'提问检查','text':'教师要求依据，模拟回应给出理由。','source_ids':['t1','t2']}]})
    runner=pipeline.ReportPipeline(Model(),tmp_path,'012345abcdef','a'*64,lambda _:None)
    asyncio.run(runner.stage('teaching_report_chunk',pipeline.ANALYZE_CHUNK,payload,AnalysisNotes,{'t1','t2'}))
    assert payload==previous, '不得改写原始课堂或角色记录'


def test_one_repair_explains_both_note_limit_and_wrong_actor_reference():
    bound=source_bound(AnalysisNotes,{'t1','t2'})
    raw={'notes':[{'kind':'要点','text':'本次实际内容。','source_ids':['r1']} for _ in range(21)]}
    with pytest.raises(ValidationError) as exc:
        bound.model_validate(raw)
    message=str(exc.value)
    assert '20' in message and 'r1' in message and 't1' in message


def test_real_ai_ids_still_cannot_support_final_teacher_judgments():
    raw=model_output();raw['overview']['source_ids']=['r1']
    with pytest.raises(ValidationError):
        source_bound(TeachingReport,{'t1','t2','t3'}).model_validate(raw)


def test_system_missing_image_is_not_a_teacher_finding():
    raw=model_output()
    raw['finding'].update(text='图片内容未提供，无法核对图中数据与估读是否准确。',source_ids=['t1'])
    turns=[{'id':'t1','role':'teacher','text':'请读当前截图的单位和数据。','image_id':'b'*64}]
    with pytest.raises(ValidationError,match='系统|报告|图片'):
        source_bound(TeachingReport,{'t1','t2','t3'},turns).model_validate(raw)


def test_actual_unreadable_image_can_still_be_a_material_improvement():
    raw=model_output()
    raw['finding'].update(text='当前小图字号过小，学生明确表示看不清刻度。',source_ids=['t1'])
    raw['action']['text']='可裁剪并放大当前图的刻度区域，再检查能否读出。'
    turns=[{'id':'t1','role':'teacher','text':'请读当前截图的刻度。','image_id':'b'*64},
           {'id':'r1','role':'ai','kind':'question','text':'字太小，刻度看不清，能放大吗？'}]
    report=source_bound(TeachingReport,{'t1','t2','t3'},turns).model_validate(raw)
    assert '字号过小' in report.finding.text


def test_actual_cropped_material_is_not_mistaken_for_report_system_limit():
    raw=model_output()
    raw['finding'].update(text='图片内容缺失，右侧受力箭头被裁掉，不能判断该物体受力。',source_ids=['t1'])
    turns=[{'id':'t1','role':'teacher','text':'请根据这张裁剪图判断受力。','image_id':'b'*64},
           {'id':'r1','role':'ai','kind':'question','text':'右侧受力箭头被裁掉了，能补全吗？'}]
    report=source_bound(TeachingReport,{'t1','t2','t3'},turns).model_validate(raw)
    assert '被裁掉' in report.finding.text


def test_primary_problem_cannot_coexist_with_all_positive_dimensions():
    raw=model_output()
    for dimension in raw['dimensions'].values():
        dimension['label']='做得好'
        dimension['source_ids']=['t1']
    raw['finding'].update(kind='首要问题',text='末尾留下错误结论，教师未回应学生的具体质疑。')
    with pytest.raises(ValidationError,match='一致|可改进'):
        TeachingReport.model_validate(raw)


def test_primary_problem_allows_specific_improvement_without_downgrading_other_dimensions():
    raw=model_output()
    for dimension in raw['dimensions'].values():
        dimension.update(label='做得好',source_ids=['t1'])
    raw['finding'].update(kind='首要问题',text='末尾留下错误结论，教师未回应学生的具体质疑。')
    raw['dimensions']['accuracy'].update(label='可改进',reason='末尾错误结论仍未更正。')
    report=TeachingReport.model_validate(raw)
    assert report.dimensions.clarity.label=='做得好'
