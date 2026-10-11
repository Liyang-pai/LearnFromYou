"""Concise report contract; source IDs are private validation metadata."""
from typing import Annotated, Literal
from pydantic import Field, StringConstraints, model_validator
from .schemas import StrictModel

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
Source = Annotated[str, StringConstraints(min_length=1, max_length=60)]


class SupportedText(StrictModel):
    text: Text
    source_ids: list[Source] = Field(min_length=1)


class Dimension(StrictModel):
    label: Literal['做得好', '可改进', '暂不能判断']
    reason: Text
    source_ids: list[Source]

    @model_validator(mode='after')
    def require_support(self):
        if self.label != '暂不能判断' and not self.source_ids:
            raise ValueError('确定判断必须有教师讲授来源')
        return self


class Dimensions(StrictModel):
    accuracy: Dimension
    clarity: Dimension
    coherence: Dimension
    checking: Dimension


class Finding(SupportedText):
    kind: Literal['首要问题', '可选提升', '材料不足']
    impact: Text

    @model_validator(mode='before')
    @classmethod
    def discard_reference_annotation(cls, value):
        # Some compatible models add this unused private annotation. Discard
        # only that field, avoiding a paid repair; claims/IDs still validate.
        if isinstance(value, dict):
            value = {key: item for key, item in value.items() if key != 'source_ids_note'}
            kind, text = value.get('kind'), value.get('text')
            # The UI/export already supplies this label. Remove only a redundant
            # leading label, preserving the actual finding and its source IDs.
            if kind in ('首要问题', '可选提升', '材料不足') and isinstance(text, str):
                text = text.strip()
                while text.startswith((kind + '：', kind + ':')):
                    text = text[len(kind) + 1:].lstrip()
                value['text'] = text
        return value


class Practice(StrictModel):
    minutes: int = Field(ge=1, le=3)
    task: Text
    criterion: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                                max_length=240, pattern=r'^教师能')]
    source_ids: list[Source] = Field(min_length=1)


class TeachingReport(StrictModel):
    overview: SupportedText
    dimensions: Dimensions
    finding: Finding
    keep: SupportedText | None
    action: SupportedText
    practice: Practice

    @model_validator(mode='after')
    def concise_body(self):
        observed = (self.dimensions.accuracy, self.dimensions.clarity, self.dimensions.coherence)
        if self.finding.kind == '材料不足' and all(dim.label != '暂不能判断' for dim in observed):
            raise ValueError('已能评价讲授内容、讲解和结构；不能仅因互动材料不足将整体发现标为材料不足。请选具体问题或可选提升')
        texts = [self.overview.text, self.finding.text, self.finding.impact,
                 self.action.text, self.practice.task, self.practice.criterion]
        texts += [d.reason for d in self.dimensions.__dict__.values()]
        if self.keep:
            texts.append(self.keep.text)
        if sum(map(len, texts)) > 400:
            raise ValueError('报告正文过长；目标250–350字，最多400字符，请精简而非删除核心结论')
        return self


class Note(SupportedText):
    kind: Literal['要点', '解释', '更正', '延期', '候选问题', '保留做法', '提问检查', '上下文']


class AnalysisNotes(StrictModel):
    notes: list[Note] = Field(min_length=1, max_length=20)


DIMENSION_NAMES = [('accuracy', '内容准确'), ('clarity', '讲解清楚'),
                   ('coherence', '结构连贯'), ('checking', '师生互动')]


def validate_sources(report, allowed):
    def walk(value):
        if isinstance(value, dict):
            if 'source_ids' in value and not set(value['source_ids']) <= allowed:
                raise ValueError('引用了不存在或不属于本阶段的教师讲授编号')
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(report.model_dump())


def source_bound(schema, allowed):
    # Reference validation participates in ModelClient's one correction attempt.
    class Bound(schema):
        @model_validator(mode='after')
        def valid_references(self):
            validate_sources(self, allowed)
            return self
    return Bound
