"""Score grounded observations with the teaching report's fixed rubric."""
import re
from typing import Annotated, Literal

from pydantic import Field

from .report_accuracy import AccuracyCheck, score_accuracy
from .report_contract import ReflectionEvidence
from .schemas import StrictModel


RubricEvidence = Annotated[list[ReflectionEvidence], Field(min_length=1, max_length=3)]


class RubricAssessment(StrictModel):
    accuracy: list[AccuracyCheck] | None = None
    clarity: dict[Literal['answer_only', 'some_steps', 'main_steps', 'example_steps',
                          'step_methods', 'contrast_example', 'decision_rule'], RubricEvidence] = Field(default_factory=dict)
    coherence: dict[Literal['opening_only', 'identifiable_topics', 'ordered_explanation', 'goal_alignment',
                            'transition_cue', 'connect_old_new', 'goal_recap'], RubricEvidence] = Field(default_factory=dict)
    interaction: dict[Literal['no_lesson_exchange', 'simple_question_or_reply', 'specific_question',
                              'addresses_question', 'understanding_question', 'followup_or_feedback',
                              'checks_reason', 'specific_reason_feedback'], RubricEvidence] = Field(default_factory=dict)
    expression: dict[Literal['unclear_key_lines', 'partial_meaning', 'main_meaning', 'consistent_terms',
                             'direct_sentences', 'clear_references', 'helpful_rephrasing'], RubricEvidence] = Field(default_factory=dict)


TIERS = {
    'clarity': (
        {'answer_only': '讲授只给出了题目或答案，缺少推理过程'},
        {'some_steps': '说出了部分解题或操作步骤'},
        {'main_steps': '说清了主要步骤，可以跟着做'},
        {'example_steps': '用例子走完了关键步骤', 'step_methods': '讲清了关键步骤各怎么做'},
        {'contrast_example': '用例子对照了容易混淆的地方', 'decision_rule': '说明了如何区分容易混淆的情况'},
    ),
    'coherence': (
        {'opening_only': '主要是开场，或听不出各段的讲解顺序'},
        {'identifiable_topics': '讲解中可以辨认出几个要点'},
        {'ordered_explanation': '能听出先讲什么、接着讲什么'},
        {'goal_alignment': '各段围绕本课目标展开', 'transition_cue': '进入新内容时给了过渡提示'},
        {'connect_old_new': '讲清了新内容与前面内容的关系', 'goal_recap': '结尾回扣了本课目标'},
    ),
    'interaction': (
        {'no_lesson_exchange': '完整记录中未见与本课有关的提问或回应'},
        {'simple_question_or_reply': '有与本课相关的接话或提问'},
        {'specific_question': '提出了具体问题', 'addresses_question': '对具体疑问或回答作了针对性回应'},
        {'understanding_question': '用具体题目检查了理解', 'followup_or_feedback': '接着小纸的回应作了追问或反馈'},
        {'checks_reason': '要求说明关键知识点的理由，并听到了理由',
         'specific_reason_feedback': '围绕该理由作了进一步提问或给出了具体反馈'},
    ),
    'expression': (
        {'unclear_key_lines': '多处关键句难以理解，或有效发言太少'},
        {'partial_meaning': '部分语句的意思能听清'},
        {'main_meaning': '主要意思表达清楚'},
        {'consistent_terms': '术语在前后讲解中保持一致', 'direct_sentences': '讲解用句直接'},
        {'clear_references': '关键表述用词和指代清楚', 'helpful_rephrasing': '需要重述时换了说法，帮助理解'},
    ),
}


RUBRIC_PROMPT = """
除内容准确另有核对规则外，四个维度只提取可核对的课堂行为，不填写数值或主观分数。程序依据以下固定条件计算得分。
在assessment字段中从低到高查找已有行为，即使行为依据不足也要返回assessment空对象。
优先保留能体现较高档行为的证据。较高档已经包含的较低档行为可由程序推出；同一档的独立条件应分别填写。
仅有部分高档条件时，也保留已证实的条件；不把没有证据、语音疑似错转、或章节笔记没提到当作低分证据。
第1档是基于完整可靠课堂确实观察到的低档表现，有第2档及以上行为就不填第1档。
每个条件提供1–3个精简且连续的逐字短引，每条含source_id、actor和quote。避免整段长引。
没有足够证据的条件请省略；某维度全部没证据就传空对象或省略该维度，不能填空数组或猜测引用。
标为pending或fallback的语音转写不作评分依据；未经审核的语音转写不作第1档依据。
整体性条件需结合全课，使用能支持判断的代表短引。涉及前后关系时引用相关两处，不只引用无关开场。
除了下述师生互动序列，每个条件都须直接引用教师原话；小纸的发言只能作为辅助背景。表达维度只引用教师实际讲解的自然语言。
followup_or_feedback依次引用实际小纸的提问或作答、教师针对该回应的追问或反馈。笼统附和或换话题不算。
先检查有没有完整的理由问答与反馈。checks_reason依次引用教师明确要求说明关键点的理由、依据或判断过程，以及实际小纸给出的理由；能否答对不是评分条件。
specific_reason_feedback在同一轮checks_reason的两条依据后，添教师针对该理由的具体追问、认可解释或纠正。
教师自问自答可作为具体设问和解释疑问的证据，不是小纸实际回应。小纸回复不能证明真实学习掌握。
各档条件如下，同档多个条件等权：
""" + '\n'.join(
    f'{name} 第{level}档：' + '；'.join(f'{key}（{description}）' for key, description in criteria.items())
    for name, tiers in TIERS.items()
    for level, criteria in enumerate(tiers, 1)
)


_EXCHANGE_ROLES = {
    'followup_or_feedback': ('ai', 'teacher'),
    'checks_reason': ('teacher', 'ai'),
    'specific_reason_feedback': ('teacher', 'ai', 'teacher'),
}
_REASON_REQUEST = re.compile(
    r'为什么|为何|(?:怎么|怎样|如何)(?:思考|想|判断|推导|得出)|'
    r'(?:说|解释|给出|讲|描述).{0,12}(?:理由|原因|依据|过程)|'
    r'(?:理由|原因|依据|过程).{0,6}(?:是什么|是啥|说一下|讲一下|呢|[?？])|\b(?:why|justify)\b', re.I)
_REASON_DECLINED = re.compile(r'(?:不要|不用|不必|无需).{0,6}(?:理由|原因|依据|过程)')

# Higher predicates can establish lower ones only when the same act demonstrates both.
_IMPLIED = {
    'main_steps': ('some_steps',),
    'example_steps': ('main_steps', 'some_steps'),
    'step_methods': ('main_steps', 'some_steps'),
    'ordered_explanation': ('identifiable_topics',),
    'goal_alignment': ('identifiable_topics',),
    'connect_old_new': ('identifiable_topics',),
    'specific_question': ('simple_question_or_reply',),
    'addresses_question': ('simple_question_or_reply',),
    'understanding_question': ('specific_question', 'simple_question_or_reply'),
    'followup_or_feedback': ('addresses_question', 'simple_question_or_reply'),
    'checks_reason': ('understanding_question', 'specific_question', 'simple_question_or_reply'),
    'main_meaning': ('partial_meaning',),
}


def _validated_anchors(criterion, evidence, by_id, turns):
    anchors = []
    validated = []
    expected = _EXCHANGE_ROLES.get(criterion)
    independent_questions = criterion in ('specific_question', 'understanding_question')
    if expected and tuple(item.actor for item in evidence[:len(expected)]) != expected:
        return None
    for item in evidence:
        index, turn = by_id.get(item.source_id, (None, {}))
        if item.actor != turn.get('role') or item.quote not in turn.get('text', ''):
            if independent_questions:
                continue
            return None
        if turn.get('mode') == 'microphone':
            review = turn.get('review_status')
            if review in ('pending', 'fallback') or (criterion in _LOW_TIERS and review != 'reviewed'):
                return None
        anchors.append(index)
        validated.append(item)

    if not expected:
        return [item for item in validated if item.actor == 'teacher'] or None
    anchors = anchors[:len(expected)]
    if anchors != sorted(set(anchors)):
        return None
    if criterion == 'followup_or_feedback':
        if turns[anchors[0]].get('kind') not in ('answer', 'question'):
            return None
    else:
        question = evidence[0].quote
        if (turns[anchors[1]].get('kind') != 'answer' or not _REASON_REQUEST.search(question)
                or _REASON_DECLINED.search(question)):
            return None
    for first, last in zip(anchors, anchors[1:]):
        if any(t['role'] == 'ai' and t.get('kind') in ('answer', 'question') for t in turns[first + 1:last]):
            return None
    return evidence[:len(expected)]


_LOW_TIERS = {next(iter(tiers[0])) for tiers in TIERS.values()}


def score_assessment(assessment: RubricAssessment | None, turns, *, incomplete=False, coverage_limited=False, lesson=None):
    by_id = {turn['id']: (index, turn) for index, turn in enumerate(turns)}
    scores = {'accuracy': score_accuracy(getattr(assessment, 'accuracy', None), turns,
                                         incomplete=incomplete, coverage_limited=coverage_limited, lesson=lesson)}
    teacher_turns = [turn for turn in turns if turn['role'] == 'teacher']
    has_reply = any(turn['role'] == 'ai' for turn in turns)
    for name, tiers in TIERS.items():
        provided = getattr(assessment, name, {})
        pending, reason = '证据不足', '现有课堂片段还不足以给这一项定档。'
        if name == 'interaction' and teacher_turns and not has_reply:
            pending, reason = '未见小纸回应', '本次记录未见小纸回应，现有证据还不足以给师生互动评分。'
        elif name == 'expression' and teacher_turns and all(turn.get('mode') == 'text' for turn in teacher_turns):
            pending, reason = '仅记录文字', '本次记录为文字讲解，现有证据还不足以给表达规范评分。'
        scores[name] = {'score': None, 'pending_label': pending, 'reason': reason, 'evidence': []}
        observations = {}
        for key, references in provided.items():
            validated = _validated_anchors(key, references, by_id, turns)
            if validated:
                observations[key] = validated
        if len(observations) != len(provided):
            scores[name]['reason'] = '有评估依据无法与课堂记录中的角色、顺序或原话对应。'
        reason_feedback = observations.get('specific_reason_feedback')
        if reason_feedback:
            reason_check = observations.get('checks_reason')
            if (reason_check and [item.source_id for item in reason_check]
                    != [item.source_id for item in reason_feedback[:2]]):
                scores[name]['reason'] = '理由检查与反馈未对应到同一轮问答。'
                del observations['specific_reason_feedback']
            else:
                observations.setdefault('checks_reason', reason_feedback[:2])
                observations.setdefault('followup_or_feedback', reason_feedback[1:])
        for higher, earlier in _IMPLIED.items():
            if higher in observations:
                teacher_anchors = [item for item in observations[higher] if item.actor == 'teacher']
                for key in earlier:
                    observations.setdefault(key, teacher_anchors)

        low = next(iter(tiers[0]))
        level, credited = (1.0, [low]) if low in observations else (None, [])
        for number, criteria in enumerate(tiers[1:], 2):
            present = [key for key in criteria if key in observations]
            if len(present) == len(criteria):
                level, credited = float(number), present
                continue
            if level is not None and present:
                level = round(level + len(present) / len(criteria), 1)
                credited += present
            break
        if level is None:
            continue

        descriptions = {key: description for criteria in tiers for key, description in criteria.items()}
        evidence = {(item.source_id, item.quote): item.model_dump()
                    for key in credited for item in observations[key]}
        scores[name] = {
            'score': level,
            'reason': '；'.join(descriptions[key] for key in credited) + '。',
            'evidence': sorted(evidence.values(), key=lambda row: by_id[row['source_id']][0]),
        }
    return scores
