"""本轮只核对已验收的报告生成页，不覆盖其他尚未同步的原型页面。"""
from test_formal_reference import object_literal, REFERENCE, FORMAL


def test_report_loading_matches_approved_prototype():
    original = object_literal(REFERENCE / 'src/screens.js')['19']
    actual = object_literal(FORMAL / 'reference.js')['19']
    assert actual['body'] == original['body'].replace('assets/', '/static/formal/assets/')
    assert '<h2>报告生成中</h2>' in actual['body']
    assert '完成后将自动展示教学报告' in actual['body']
    assert 'aria-label="报告正在生成"' in actual['body']
    assert '<span>生成报告</span>' not in actual['body']
