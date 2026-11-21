"""Bind knowledge judgments to classroom statements and score the final teaching."""
import re
from itertools import groupby
from typing import Annotated, Literal
from unicodedata import normalize

from pydantic import Field

from .report_calculation import check_list_depth, list_depth_facts
from .report_contract import ReflectionEvidence, Text
from .schemas import StrictModel


class AccuracyClaim(StrictModel):
    claim: Text
    core: bool
    evidence: Annotated[list[ReflectionEvidence], Field(min_length=1, max_length=3)]


class AccuracyJudgment(StrictModel):
    topic: Text
    basis: Text
    verdict: Literal['correct', 'imprecise', 'secondary_error', 'key_error', 'core_error', 'unverified']
    corrects_earlier: bool = False


class AccuracyCheck(AccuracyClaim, AccuracyJudgment):
    pass


ACCURACY_PROMPT = """
内容准确：核对本课实际讲授的定义、关键规则、适用条件和例题结论。逐条给出claim、是否属于本课核心(core)、判定(verdict)及可复核的依据(basis)。未讲授的目标、提问、假设、用来辨析的反例和因学段合理简化的条件本身不扣分。
同一知识点及适用范围内的说法使用逐字相同的简短topic，例如都用“广义表深度”，包括原说法、明确更正和更正后再次讲错。topic只写知识点，去掉“原说法”“错误”“更正”等阶段词。不同适用条件分别取topic。教师明确改正或解释了原说法时，在更正项填corrects_earlier=true。
证据必须来自真实教师原话。同一句在一个片段内重复时连同邻近语句引用，使短引能唯一定位。小纸的作答只作背景；教师明确采用其中结论时，引用作答及随后明确认可该结论的教师原话。
每条先填basis完成独立核对，再填verdict。教师原话用于确定待核对的说法。basis应写出可独立复核的依据，单写“教师说法正确”或复述原话都不够。算例先从题设和学科定义算出正确结果，在basis写出决定性的一步，然后与教师结论对照。同一题的复算结果与教师结论冲突时判为错误。同一错误规则及由此得到的算错归到同一个topic。
遇到层数、深度、总数等易混量，分别计算。计算最大嵌套层数时，每条路径都计入最外层，再取最大值；并列的括号增加括号总对数。规则可用反例核对。稳定知识写出规则与适用条件。文件上传状态与评分无关。
若输入有verified_list_depths，它是程序从教师给出的显式广义表算出的结果。pairs是括号总对数，max_depth是最大嵌套层数。核对同一算式时使用这些值，并引用教师报出结论的原话。
含糊说法先找教师是否在前后明确限定含义。若原话既可理解为对某个例子的描述，也可理解为另一条通用规则，且上下文无法确定，判为unverified并具体指出歧义。例如从“有多个括号”直接说到“比如两个括号”且未说明并列情况，需核实是只谈这个例子还是在讲通则。保留待核实的核心说法，也分别列出能够独立核对的清楚说法。
特定教材口径、法规、时效数据、未经提供的图片内容或疑似转写错误，依据不足时用unverified并说明缺少什么。不要编造来源。pending或fallback转写也用unverified。
verdict使用correct（核对通过）、imprecise（只影响术语精度）、secondary_error（次要事实错误）、key_error（关键例题或实质适用条件错误）、core_error（核心定义、规则或结论错误）、unverified（待核实）。模型填写判定，程序计算分数。
正文若有内容准确方面的改进建议，只引用最终仍有偏差的同一核对项，原句与核对项保持一致；其余提示集中在核对详情。
"""

SCORE_CAPS = {'imprecise': 4.5, 'secondary_error': 4.0, 'key_error': 3.0, 'core_error': 2.0}
LABELS = {
    'correct': '核对通过', 'imprecise': '术语表述有偏差', 'secondary_error': '次要事实错误',
    'key_error': '关键例题或适用条件错误', 'core_error': '核心知识错误', 'unverified': '待核实',
}
ADOPTS_ANSWER = re.compile(
    r'(?:^|[，。！？,!?]\s*)(?:对(?:[，。！、,.!\s]|$)|确实|没错|是的|你(?:说|答|算)得对|'
    r'(?:这个|这道题的)(?:答案|结论)(?:是对的|正确|成立|没问题)|(?:我)?(?:同意|采用)(?:这个|你的|小纸的)|'
    r'(?:就|我们)按(?:你的|你说的|这个))')
REJECTS_ANSWER = re.compile(r'不对|不正确|不成立|不准确|不(?:同意|赞同)|有误|错了')
TOPIC_STAGE_SUFFIX = re.compile(r'\s*\((?:初始说法|原说法|原表述|错误(?:说法|表述)?|(?:更正|纠正|修正)(?:后)?)\)$')
BRACKET_EXAMPLE_SCOPE = re.compile(r'多[个对]括号.{0,12}(?:比如|例如).{0,8}(?:两|二|2)[个对]括号')
CLARIFIES_BRACKET_SCOPE = re.compile(r'并列|同一?层|最大(?:括号)?嵌套|只看这(?:个|道)|这个例子中|外层.{0,18}内层')
CLAIM_BREAK = re.compile(r'[。！？!?；;\n]|(?<!\d)[.,]|[.,](?!\d)')


def statement_key(text):
    return ' '.join(normalize('NFKC', text).casefold().split()).rstrip('。.,;:!?') or text


def evidence_position(item, by_id):
    index, turn = by_id.get(item.source_id, (-1, {}))
    if item.actor == turn.get('role'):
        text = turn['text']
        start = text.find(item.quote)
        if start >= 0 and text.find(item.quote, start + 1) < 0:
            return index, start
    return None


def final_judgment(observations):
    errors, correction = [], None
    ordered = sorted(observations, key=lambda entry: entry[0])
    core = any(row['core'] for _, row, _ in ordered)
    for _, at_position in groupby(ordered, key=lambda entry: entry[0]):
        same_statement = list(at_position)
        _, row, explicit = same_statement[-1]
        if any(other['verdict'] != row['verdict'] for _, other, _ in same_statement):
            row.update(verdict='unverified', basis='同一句课堂原话有不同的核对结论，需进一步核实。')
        if row['verdict'] == 'unverified':
            continue
        if explicit and errors:
            correction = (errors[-1][1], row)
            errors.clear()
        if row['verdict'] in SCORE_CAPS:
            errors.append((SCORE_CAPS[row['verdict']], row))
    earlier_error = None
    if errors:
        _, worst = min(reversed(errors), key=lambda entry: entry[0])
        if row['verdict'] == 'unverified':
            earlier_error = {**worst, 'core': core}
        else:
            row = worst
    elif correction and row['verdict'] == 'correct':
        earlier, revised = correction
        row.update(corrected=True, evidence=[*earlier['evidence'], *revised['evidence']])
    return {**row, 'core': core}, earlier_error


def core_error_anchors(row, claim):
    topic = statement_key(row['topic'])
    for ref in row['evidence']:
        if ref['actor'] != 'teacher':
            continue
        quote = statement_key(ref['quote'])
        if quote.count(claim) == 1:
            yield ref['source_id'], claim
            continue
        clauses = [part.strip() for part in CLAIM_BREAK.split(quote) if part.strip()]
        if len(clauses) == 1:
            yield ref['source_id'], quote
            continue
        matches = [part for part in clauses if topic in part or part in claim]
        if len(matches) == 1:
            yield ref['source_id'], matches[0]


def add_judgment(rows, errors, row):
    if row['verdict'] == 'core_error':
        claim = statement_key(row['claim'])
        quotes = list(core_error_anchors(row, claim))
        for prior_claim, prior_quotes in errors:
            if claim == prior_claim or any(
                quote == old or (source == source_id and (quote in old or old in quote))
                for source, quote in quotes for source_id, old in prior_quotes
            ):
                return False
        errors.append((claim, quotes))
    rows.append(row)
    return True


def score_accuracy(checks, turns, *, incomplete=False, coverage_limited=False, lesson=None):
    if checks is None:
        return {'score': None, 'pending_label': '未核对', 'reason': '这份报告没有内容准确的核对记录。', 'evidence': []}

    by_id = {turn['id']: (index, turn) for index, turn in enumerate(turns)}
    depth_facts = list_depth_facts(turns, lesson)
    grouped, unsupported = {}, []
    for check in checks:
        row = {'topic': check.topic, 'claim': check.claim, 'core': check.core or check.verdict in ('core_error', 'key_error'),
               'verdict': check.verdict, 'basis': check.basis, 'evidence': [], 'corrected': False}
        located = [(evidence_position(item, by_id), item) for item in check.evidence]
        teachers = [(position, item) for position, item in located if position is not None and item.actor == 'teacher']
        replies = [position for position, item in located if position is not None and item.actor == 'ai']
        if (not teachers or any(position is None for position, _ in located)
                or (replies and not any(at > max(replies) and ADOPTS_ANSWER.search(item.quote)
                                       and not REJECTS_ANSWER.search(turns[at[0]]['text'])
                                       for at, item in teachers))):
            row.update(topic='课堂原话待核实', claim='无法定位的课堂说法', verdict='unverified',
                       basis='缺少可唯一定位的教师原话，或发言无法支持归属。')
            unsupported.append(row)
            continue

        ordered = sorted(located, key=lambda pair: pair[0])
        row['evidence'] = [item.model_dump() for _, item in ordered]
        teacher_turns = [turns[index] for (index, _), _ in teachers]
        if any(turn.get('mode') == 'microphone' and turn.get('review_status') in ('pending', 'fallback')
               for turn in teacher_turns):
            row.update(verdict='unverified', basis='这段教师语音转写待核实。')
        elif row['verdict'] in SCORE_CAPS and any(
                turn.get('mode') == 'microphone' and turn.get('review_status') != 'reviewed'
                for turn in teacher_turns):
            row.update(verdict='unverified', basis='这段语音转写未经审核，需核对原话后再判断偏差。')
        elif row['verdict'] == 'correct':
            classroom_words = ' '.join(turn['text'] for turn in teacher_turns)
            if BRACKET_EXAMPLE_SCOPE.search(classroom_words) and not CLARIFIES_BRACKET_SCOPE.search(classroom_words):
                row.update(verdict='unverified',
                           basis='原话从多个括号举到两个括号，需核实是在说明这个例子，还是在给出通则。并列子表也会增加括号对数。')
        if depth_facts and row['verdict'] != 'unverified':
            position, item = max(teachers, key=lambda anchor: anchor[0])
            verified = check_list_depth(item.quote, position, turns, depth_facts)
            if verified:
                verdict, basis = verified
                if verdict == 'correct' and row['verdict'] == 'correct':
                    row['basis'] = basis
                elif verdict != 'correct':
                    if any(turn.get('mode') == 'microphone' and turn.get('review_status') != 'reviewed'
                           for turn in teacher_turns):
                        row.update(verdict='unverified', basis='复算与转写结论有差异，需核对教师原话。' + basis)
                    else:
                        worst = min((verdict, row['verdict']), key=lambda value: SCORE_CAPS.get(value, 5.0))
                        row.update(verdict=worst, basis=basis, core=True)
        name = statement_key(check.topic)
        key = TOPIC_STAGE_SUFFIX.sub('', name).strip() or name
        grouped.setdefault(key, []).append((max(position for position, _ in teachers), row, check.corrects_earlier))

    rows, summaries, counted_core_errors = [], [], []
    for observations in sorted(grouped.values(), key=lambda entries: max(entry[0] for entry in entries)):
        row, earlier_error = final_judgment(observations)
        if earlier_error:
            add_judgment(rows, counted_core_errors, earlier_error)
        if add_judgment(rows, counted_core_errors, row):
            summaries.append(row)
    rows.extend(unsupported)
    summaries.extend(unsupported)
    for row in rows:
        row['label'] = '课堂中已更正' if row['corrected'] else LABELS[row['verdict']]

    core = [row for row in summaries if row['core']]
    verified = sum(row['verdict'] != 'unverified' for row in core)
    preview = next((row for row in core if row['verdict'] == 'unverified'), None)
    if preview is None:
        preview = next(iter(core or rows), None)
    pending_secondary = sum(not row['core'] and row['verdict'] == 'unverified' for row in summaries)
    scope = f'核心说法已核对 {verified}/{len(core)} 项。' if core else ''
    if pending_secondary:
        scope += f'另有 {pending_secondary} 项次要说法待核实。'
    score, pending_label = None, None
    if incomplete:
        pending_label = '记录不完整'
        reason = '课堂记录有缺段，整课准确性暂缓给分。'
    elif coverage_limited:
        pending_label = '核对未完成'
        reason = '本次核对尚未覆盖全部核心说法，整课准确性暂缓给分。'
    elif not rows:
        pending_label = '暂无可核对说法'
        reason = '课堂记录中没有可核对的知识说法。'
    elif not core:
        pending_label = '核心证据不足'
        reason = '课堂记录中缺少可核对的核心说法。'
    elif verified < len(core):
        pending_label = f'已核对 {verified}/{len(core)}' if verified else '核心待核实'
        reason = '核心说法中还有待确认的内容，整维暂缓给分。'
    else:
        worst = min(rows, key=lambda row: SCORE_CAPS.get(row['verdict'], 5.0))
        score = 1.0 if len(counted_core_errors) >= 2 else SCORE_CAPS.get(worst['verdict'], 5.0)
        if score < 5.0:
            preview = worst
        reason = ('有两项及以上独立的核心知识错误。' if score == 1.0 else
                  f"本次核对发现{worst['label']}。" if score < 5.0 else '核心说法核对通过。')
    result = {'score': score, 'reason': scope + reason, 'evidence': [], 'checks': rows}
    if preview is not None:
        result['preview_check_index'] = rows.index(preview)
    if pending_label:
        result['pending_label'] = pending_label
    return result
