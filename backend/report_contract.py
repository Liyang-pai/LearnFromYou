"""Concise report contract; source IDs are private validation metadata."""
from typing import Annotated, Literal
import re
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


class ActionEvidence(StrictModel):
    """Internal attribution, never rendered in the report or Markdown."""
    actor: Literal['teacher', 'ai']
    action: Literal['request', 'demonstration', 'feedback', 'answer']
    source_id: Source
    quote: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class TeachingReport(StrictModel):
    overview: SupportedText
    dimensions: Dimensions
    finding: Finding
    keep: SupportedText | None
    action: SupportedText
    practice: Practice
    action_evidence: list[ActionEvidence] = Field(default_factory=list, max_length=6)

    @model_validator(mode='after')
    def concise_body(self):
        if (self.finding.kind == '首要问题'
                and not any(dim.label == '可改进' for dim in self.dimensions.__dict__.values())):
            raise ValueError('报告结论不一致：已列首要问题，相关维度须说明该未解决障碍并标为可改进；'
                             '若实际只是可选提升，请核对全课后修改发现，不能凭空制造缺点')
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


def invalid_sources(value, allowed):
    invalid = set()
    def walk(value):
        if isinstance(value, dict):
            ids = value.get('source_ids')
            if isinstance(ids, list):
                invalid.update(s for s in ids if isinstance(s, str) and s not in allowed)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(value)
    return invalid


def validate_sources(report, allowed):
    invalid = invalid_sources(report.model_dump(), allowed)
    if invalid:
        raise ValueError('引用了不存在或不属于本阶段的教师讲授编号：' + ', '.join(sorted(invalid)) +
                         '；仅可使用：' + ', '.join(sorted(allowed)))


def worked_example_sources(turns):
    """Unambiguous numeric workings already spoken by the teacher, not AI."""
    found = []
    for turn in turns:
        if turn['role'] != 'teacher':
            continue
        for sentence in re.findall(r'[^。！？\n]+[。！？]?', turn['text']):
            sentence = sentence.strip()
            if sentence.endswith(('？', '?')) or re.match(r'^(?:请|写出|回答|算出|求出|你能|不要)', sentence):
                continue
            operation = re.search(r'\d[\d.]*\s*[×÷*/+−-]\s*\d[\d.]*', sentence)
            if operation and re.search(r'(?:结果|得到|得出|等于)\s*-?\d|[=＝]\s*-?\d', sentence[operation.end():]):
                found.append(turn['id'])
                break
    return found


def interaction_facts(turns):
    replies = [turn for turn in turns if turn['role'] == 'ai']
    last = max((i for i, turn in enumerate(turns) if turn['role'] == 'ai'), default=-1)
    completed = []
    for question, answer, feedback in zip(turns, turns[1:], turns[2:]):
        if (question['role'] == feedback['role'] == 'teacher' and answer['role'] == 'ai'
                and answer.get('kind') == 'answer'
                and re.search(r'为什么|(?:请|要求).*(?:理由|依据)', question['text'])
                and not re.search(r'(?:不要|不必|不用|无需).{0,6}(?:理由|依据)', question['text'])
                and re.search(r'因为|依据|所以', answer['text'])
                and not re.search(r'不正确|不对|有误|错误|未说明|没说明', feedback['text'])
                and re.search(r'^(?:对[，。！]|正确|答对|是的)|(?:理由|依据|过程).{0,8}正确', feedback['text'])):
            completed.append({'teacher_question_id': question['id'], 'ai_answer_id': answer['id'],
                              'teacher_feedback_id': feedback['id']})
    # Facts are calibration metadata, not another copy of the entire lesson.
    return {'delivered_ai_reply_ids': [turn['id'] for turn in replies][-8:],
            'delivered_ai_answer_ids': [turn['id'] for turn in replies if turn.get('kind') == 'answer'][-8:],
            'completed_reason_checks': completed[-4:],
            'teacher_worked_example_ids': worked_example_sources(turns)[-8:],
            'teacher_after_last_reply_ids': [turn['id'] for turn in turns[last + 1:]
                                             if last >= 0 and turn['role'] == 'teacher'][-8:]}


def validate_grounding(report, turns):
    """Check observable facts/quoted attribution, not general teaching quality."""
    by_id = {turn['id']: turn for turn in turns}
    facts = interaction_facts(turns)
    if any(turn.get('image_id') for turn in turns):
        system_limit = (r'(?:本次报告|本报告|报告模块|系统|分析端).{0,12}'
                        r'(?:未收到|未获得|看不到|无法(?:查看|读取|核对))|'
                        r'(?:图片|截图|图像)(?:内容|原图)(?:未提供|未收到).{0,12}'
                        r'(?:无法|不能|难以).{0,6}(?:核对|评价|判断|验证)')
        if re.search(system_limit, report.finding.text):
            raise ValueError('课堂已经展示截图；报告未收到原图是系统评价边界，不能作为教师的教学发现。'
                             '请评价已有文字支持的教学动作，或保留并精简已有效的做法')
    observed = [report.overview, *report.dimensions.__dict__.values(), report.finding]
    if report.keep:
        observed.append(report.keep)
    # Wording cannot prove an unsupported judgment: "学生追问…教师未澄清"
    # describes a specific interaction even without the word "疑问". Keep
    # pedagogical calibration in the prompt; hard checks below need evidence.
    if (facts['completed_reason_checks'] and report.finding.kind == '可选提升'
            and re.search(r'为什么|(?:请|让|问|要求).*(?:理由|依据)', report.action.text)
            and not re.search(r'保持|保留|精简|继续|压缩|误解|错误|未解释|不清', report.finding.text)):
        raise ValueError('已经完成要求理由、AI说明理由、教师反馈的检查；不能将重复该步骤当作新提升，可保留并精简现有做法')
    evidence = worked_example_sources(turns)
    for item in report.action_evidence:
        source = by_id.get(item.source_id)
        if not source or source['role'] != item.actor or item.quote not in source['text']:
            raise ValueError('动作依据必须逐字来自对应角色的实际发言，不能将AI回答归为教师示范')
        if item.action == 'demonstration' and item.actor == 'teacher':
            prefix = re.split(r'[。！？\n]', source['text'][:source['text'].index(item.quote)])[-1]
            if re.match(r'^(?:请|写出|回答|告诉我|说说|算出|求出|试着|你能|能不能)', (prefix + item.quote).strip()):
                raise ValueError('教师布置任务不是教师示范；请删除不成立的示范评价，而非编造原文')
            evidence.append(item.source_id)
    for part in observed:
        text = getattr(part, 'text', getattr(part, 'reason', ''))
        # "No real student response" and "no teacher feedback" remain valid.
        for match in re.finditer(r'(?:没有|未见|无|缺少|未提供|未记录)(?:实际|任何|课堂|相应)?'
                                 r'(?:AI学生|AI|学生)(?:的)?(回应|发言|回答)', text):
            present = facts['delivered_ai_answer_ids'] if match[1] == '回答' else facts['delivered_ai_reply_ids']
            if present:
                raise ValueError('输入已有实际AI回应，不能宣称没有回应；可分别说明教师是否反馈，不能推断真实学生掌握')
        # Only retrospective claims, never the future action/practice sections.
        for clause in re.split(r'[，。；;]', text):
            if not re.search(r'示范|演示|演算', clause):
                continue
            if re.search(r'^(?:可|建议|下次|避免|不妨)|未|没有|缺少|不足|应补|需补', clause):
                continue
            if ('AI' in clause or '学生' in clause) and '教师' not in clause and '老师' not in clause:
                continue
            if not set(part.source_ids) & set(evidence):
                raise ValueError('声称教师示范须在action_evidence提供相关教师原句；AI计算和教师要求不能作为示范依据')


def source_bound(schema, allowed, turns=None):
    # Reference validation participates in ModelClient's one correction attempt.
    class Bound(schema):
        @model_validator(mode='before')
        @classmethod
        def notes_repair_details(cls, value):
            # Report all independently visible note problems in the one repair
            # budget; a list-length error must not hide wrong-role references.
            if issubclass(schema, AnalysisNotes) and isinstance(value, dict):
                notes = value.get('notes')
                errors = []
                if isinstance(notes, list) and len(notes) > 20:
                    errors.append(f'笔记最多20条，当前{len(notes)}条；按主题合并，不得丢弃后半段或更正')
                invalid = invalid_sources(value, allowed)
                if invalid:
                    errors.append('来源不能使用' + ', '.join(sorted(invalid)) +
                                  '；仅可使用本阶段教师编号：' + ', '.join(sorted(allowed)))
                if errors:
                    raise ValueError('；'.join(errors))
            return value

        @model_validator(mode='after')
        def valid_references(self):
            validate_sources(self, allowed)
            if turns is not None and isinstance(self, TeachingReport):
                validate_grounding(self, turns)
            return self
    return Bound
