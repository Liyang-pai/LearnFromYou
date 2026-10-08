"""课后报告 V2 的独立结构契约，不扩展课堂认知或测验状态。"""
from typing import Literal
from pydantic import Field
from .schemas import StrictModel


class Citation(StrictModel):
    event_id: str = Field(min_length=1, max_length=80)
    quote: str = Field(min_length=1, max_length=600)


class RecallItem(StrictModel):
    text: str = Field(min_length=1, max_length=500)
    knowledge_ids: list[str] = Field(default_factory=list, max_length=6)
    question_ids: list[str] = Field(default_factory=list, max_length=6)
    citations: list[Citation] = Field(min_length=1, max_length=6)


class Probe(StrictModel):
    id: str = Field(pattern=r'^v[1-3]$')
    knowledge_id: str = Field(min_length=1, max_length=60)
    question: str = Field(min_length=1, max_length=300)
    citations: list[Citation] = Field(min_length=1, max_length=4)
    task_kind: Literal['解释原因', '判断新情况', '发现错误', '迁移应用', '未标注'] = '未标注'
    required_tasks: list[str] = Field(default_factory=list, max_length=3)


class RecallPlan(StrictModel):
    explained: list[RecallItem] = Field(default_factory=list, max_length=6)
    doubts: list[RecallItem] = Field(default_factory=list, max_length=6)
    uncertain: list[RecallItem] = Field(default_factory=list, max_length=6)
    probes: list[Probe] = Field(default_factory=list, max_length=3)


class ProbeAnswer(StrictModel):
    id: str = Field(pattern=r'^v[1-3]$')
    text: str = Field(min_length=1, max_length=600)
    citations: list[Citation] = Field(default_factory=list, max_length=5)


class Answers(StrictModel):
    answers: list[ProbeAnswer] = Field(max_length=3)


class TaskCheck(StrictModel):
    task: str = Field(min_length=1, max_length=300)
    outcome: Literal['有依据地完成', '未回答', '部分完成', '理由错误', '无法判断']
    answer_quote: str = Field(default='', max_length=600)
    explanation: str = Field(min_length=1, max_length=500)
    citations: list[Citation] = Field(default_factory=list, max_length=4)


class ReasoningCheck(StrictModel):
    kind: Literal['新情境推理', '仅换名换数或复述', '无法判断']
    answer_quote: str = Field(default='', max_length=600)
    explanation: str = Field(min_length=1, max_length=500)
    citations: list[Citation] = Field(default_factory=list, max_length=4)


class Verdict(StrictModel):
    id: str = Field(pattern=r'^v[1-3]$')
    status: Literal['有理解证据', '存在误解', '尚未验证', '证据不足']
    reasoning: Literal['解释或应用', '机械复述', '无法判断']
    explanation: str = Field(min_length=1, max_length=600)
    citations: list[Citation] = Field(default_factory=list, max_length=6)
    task_checks: list[TaskCheck] = Field(default_factory=list, max_length=3)
    reasoning_check: ReasoningCheck | None = None


class Verification(StrictModel):
    results: list[Verdict] = Field(max_length=3)


class Finding(StrictModel):
    id: str = Field(pattern=r'^f[1-6]$')
    observation: str = Field(min_length=1, max_length=500)
    interpretation: str = Field(min_length=1, max_length=500)
    citations: list[Citation] = Field(min_length=1, max_length=6)


class Suggestion(StrictModel):
    finding_id: str = Field(pattern=r'^f[1-6]$')
    priority: Literal['优先', '其次']
    action: str = Field(min_length=1, max_length=600)


class Diagnosis(StrictModel):
    strengths: list[Finding] = Field(default_factory=list, max_length=3)
    weaknesses: list[Finding] = Field(default_factory=list, max_length=3)
    suggestions: list[Suggestion] = Field(default_factory=list, max_length=3)
