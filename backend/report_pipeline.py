"""Ordered long-class analysis with durable, input-addressed stage results."""
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from pydantic import Field

from .llm import ModelError
from .report_accuracy import ACCURACY_PROMPT, AccuracyCheck, AccuracyClaim, AccuracyJudgment, statement_key
from .report_calculation import list_depth_facts
from .report_contract import AnalysisNotes, ReflectionReport, evidence_turns, source_bound, interaction_facts
from .report_prompt import (ANALYZE_CHUNK, MERGE_NOTES, REFLECTION_REPORT, REFLECTION_NOTES,
                            FILLER_COUNTS_PROMPT, TYPED_FILLER_COUNTS_PROMPT)
from .report_scoring import RUBRIC_PROMPT, RubricAssessment
from .schemas import StrictModel

SHORT_LIMIT = 24000
CHUNK_LIMIT = 12000
# Short, dense Q/A turns can exceed the notes budget long before the text limit.
CHUNK_TEACHER_LIMIT = 8


class FillerCount(StrictModel):
    word: str
    count: int = Field(strict=True)


class ScoredReflectionReport(ReflectionReport):
    assessment: RubricAssessment
    accuracy_coverage_limited: bool = False
    word_counts: list[FillerCount] | None = None


class AccuracyChunkNotes(AnalysisNotes):
    accuracy_claims: list[AccuracyClaim] | None = None
    word_counts: list[FillerCount] | None = None


class AccuracyReference(AccuracyJudgment):
    claim_id: int = Field(ge=1)


class LongAssessment(RubricAssessment):
    accuracy: list[AccuracyReference] | None = None


class LongScoredReflectionReport(ReflectionReport):
    assessment: LongAssessment


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def compact(classroom):
    # 报告评价实际讲授，完整画像仅保存在原始课堂记录中，不占用分析窗口。
    lesson = {key: value for key, value in classroom['lesson'].items()
              if key in ('topic', 'points', 'grade', 'subject', 'student_level', 'lesson_type', 'textbook')}
    if any(turn.get('image_id') for turn in classroom['turns']):
        lesson['has_visual_material'] = True
    turns = [
        {key: turn[key] for key in ('id', 'role', 'text', 'mode', 'kind', 'image_id',
                                  'question_status', 'resolution_sources') if key in turn}
        for turn in evidence_turns(classroom)]
    return {'lesson': lesson, 'interaction_facts': interaction_facts(classroom['turns']), 'turns': turns}


def count_fillers(rows, turns):
    teacher_texts = [turn['text'] for turn in turns if turn['role'] == 'teacher' and not turn.get('context_only')]
    if rows is None and any(teacher_texts):
        return None
    counts = Counter()
    for item in rows or ():
        word = item.word.strip()
        if not (1 <= len(word) <= 7 and word[0].isalnum() and word[-1].isalnum() and item.count > 0):
            continue
        # The teacher's text bounds the model's count, which includes only filler uses.
        if item.count <= sum(text.count(word) for text in teacher_texts):
            counts[word] = max(counts[word], item.count)
    return counts


def rank_filler_counts(counts):
    if counts is None:
        return None
    return [FillerCount(word=word, count=count) for word, count in counts.most_common(4) if count >= 2]


def split_text(text, limit=2000):
    """Prefer sentence boundaries; hard-split a huge sentence without losing characters."""
    while len(text) > limit:
        boundaries = list(re.finditer(r'[。！？!?\n]', text[:limit]))
        end = boundaries[-1].end() if boundaries else limit
        yield text[:end]
        text = text[end:]
    if text:
        yield text


def chunks(payload):
    lesson = payload['lesson']
    if len(encode(lesson)) > CHUNK_LIMIT // 2:
        raise ModelError('教学设置过长，无法在分析窗口中完整保留；请缩短设置后重试')
    current, previous_teacher = [], None
    for turn in payload['turns']:
        for text in split_text(turn['text']):
            part = {**turn, 'text': text}
            candidate = {'lesson': lesson, 'turns': [*current, part]}
            teachers = sum(t['role'] == 'teacher' and not t.get('context_only') for t in current)
            dense = part['role'] == 'teacher' and teachers >= CHUNK_TEACHER_LIMIT
            if current and (len(encode(candidate)) > CHUNK_LIMIT or dense):
                preceding_reply = current[-1] if current[-1]['role'] == 'ai' else None
                yield {'lesson': lesson, 'turns': current}
                current = []
                if part['role'] == 'ai' and previous_teacher:
                    # Context was already analyzed in full. Repeating it makes an
                    # AI-only continuation interpretable without inventing a source.
                    current.append({**previous_teacher, 'context_only': True})
                elif part['role'] == 'teacher' and preceding_reply:
                    current.append({**preceding_reply, 'context_only': True})
            current.append(part)
            if part['role'] == 'teacher' and part['text']:
                previous_teacher = part
    if current:
        yield {'lesson': lesson, 'turns': current}


def note_sources(payload):
    return {sid for group in payload['groups'] for note in group['notes'] for sid in note['source_ids']}


def batches(groups):
    current = []
    for group in groups:
        if current and len(encode({'groups': [*current, group]})) > CHUNK_LIMIT:
            yield current
            current = []
        current.append(group)
    if current:
        yield current


def notes_input(payload, allowed, *, reflection=False):
    """Keep AI speech and its order, but expose teacher anchors as citable IDs."""
    result = {**payload, 'allowed_teacher_source_ids': sorted(allowed)}
    if 'turns' in payload:
        turns, previous = [], []
        for turn in payload['turns']:
            if turn['role'] == 'teacher':
                previous = [turn['id']]
                turns.append(dict(turn))
            else:
                turns.append({**{k: v for k, v in turn.items() if k != 'id'},
                              'teacher_source_ids': list(previous)})
                if reflection:
                    turns[-1]['reply_id'] = turn['id']
        result['turns'] = turns
    return result


def chunk_accuracy_claims(part, candidates):
    by_id = {}
    for turn in part['turns']:
        by_id.setdefault(turn['id'], []).append(turn)
    claims, limited = [], candidates is None
    for claim in candidates or ():
        if any(item.actor == 'teacher' for item in claim.evidence) and all(
            any(turn['role'] == item.actor and item.quote in turn['text']
                for turn in by_id.get(item.source_id, ()))
            for item in claim.evidence
        ):
            claims.append(claim)
        else:
            limited |= claim.core
    return claims, limited


def accuracy_context(claims, turns):
    order = {turn['id']: (index, turn['text']) for index, turn in enumerate(turns)}
    ordered = sorted(claims, key=lambda claim: max(
        (order[item.source_id][0], order[item.source_id][1].find(item.quote))
        for item in claim.evidence if item.actor == 'teacher'
    ))
    repetitions = {}
    for number, claim in enumerate(ordered, 1):
        key = (claim.core, statement_key(claim.claim),
               tuple((item.actor, statement_key(item.quote)) for item in claim.evidence))
        first, _ = repetitions.get(key, (number, number))
        repetitions[key] = (first, number)
    keep = {number for occurrences in repetitions.values() for number in occurrences}
    selected, size, limited = {}, 0, False
    # shortcut: bound the final verdict list under the existing output budget; use another
    # verification batch if lesson-wide coverage of more than 20 distinct inputs is needed.
    for number in sorted(keep, key=lambda index: (not ordered[index - 1].core, -index)):
        claim = ordered[number - 1]
        item = {'id': number, **claim.model_dump()}
        length = len(encode(item)) + 1
        if len(selected) >= 20 or size + length > CHUNK_LIMIT:
            limited |= claim.core
            continue
        selected[number] = item
        size += length
    return [selected[number] for number in sorted(selected)], limited


def teacher_excerpts(turns, allowed):
    excerpts, size = [], 0
    for turn in turns:
        if turn['role'] != 'teacher' or turn['id'] not in allowed:
            continue
        excerpt = {'source_id': turn['id'], 'quote': turn['text'][:120]}
        size += len(encode(excerpt)) + 1
        if size > CHUNK_LIMIT:
            break
        excerpts.append(excerpt)
    return excerpts


def bind_accuracy_result(report, claims, limited):
    judgments = {}
    for check in report.assessment.accuracy or ():
        judgments.setdefault(check.claim_id, []).append(check.model_dump(exclude={'claim_id'}))
    checks = []
    for claim in claims:
        number = claim['id']
        proposed = judgments.get(number, [])
        judgment = proposed[0] if proposed and all(other == proposed[0] for other in proposed) else {
            'topic': claim['claim'], 'verdict': 'unverified', 'basis': '未得到对应课堂原话的一致核对结果。',
        }
        checks.append({**{key: value for key, value in claim.items() if key != 'id'}, **judgment})
    result = report.model_dump()
    result['assessment']['accuracy'] = checks
    return ScoredReflectionReport.model_validate({**result, 'accuracy_coverage_limited': limited})


class ReportPipeline:
    def __init__(self, model, root: Path, sid, digest, progress):
        self.model, self.progress = model, progress
        self.has_visual_material = False
        self.turns = None
        # Keep paths short on Windows; full hashes in each file still guard reuse.
        self.directory = root / 'logs' / f'{sid}.report-v2-stages' / digest[:16]
        self.directory.mkdir(parents=True, exist_ok=True)

    async def stage(self, phase, prompt, payload, schema, allowed, *, reflection=False):
        if phase in ('teaching_report_chunk', 'teaching_report_merge'):
            payload = notes_input(payload, allowed, reflection=reflection)
        if self.has_visual_material:
            prompt += ('\n本课堂包含实际展示的截图，但本次报告没有收到图片内容。'
                       '不能将没有口述图中内容判为漏讲，也不能臆测图里的知识错误、解释或答案。'
                       '图片缺失不等于AI回应缺失：实际AI文字仍在turns或笔记中，必须按角色读取。'
                       '可评价教师设问及回应处理，但不能仅凭AI复述就认定图中公式正确，'
                       '更不能把AI计算写成教师示范。不能独立核对的图片内容应在对应维度保守判断，'
                       '不要将局部限制扩大成整次课堂材料不足。'
                       '用户确实已上传截图；报告自身未收到原图是系统边界，绝不能放入finding、'
                       '影响、改法或练习，不能说用户没有提供图片，也不能要求教师重复口述来补偿。'
                       '例如已要求先读坐标轴和单位且得到相应回应，就肯定并精简该做法，'
                       '不要再把先读坐标轴、单位作为新增提升。若实际AI明确表示图中文字太小，'
                       '可依据真实回应建议裁剪放大，这与报告自身看不到图不同。')
        bound = source_bound(schema, allowed, self.turns)
        key = hashlib.sha256(encode({'phase': phase, 'prompt': prompt, 'payload': payload,
                                    'schema': schema.model_json_schema()}).encode('utf-8')).hexdigest()
        path = self.directory / (key[:24] + '.json')
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding='utf-8'))
                if saved['key'] == key:
                    return bound.model_validate(saved['result'])
            except (ValueError, TypeError, KeyError):
                pass  # A damaged stage is regenerated; other valid stages remain usable.
        result = await self.model.generate(phase, prompt, payload, bound)
        result = bound.model_validate(result.model_dump())
        temporary = path.with_suffix('.tmp')
        temporary.write_text(encode({'key': key, 'result': result.model_dump()}), encoding='utf-8')
        temporary.replace(path)
        return result

    async def generate(self, classroom):
        payload = compact(classroom)
        self.turns = evidence_turns(classroom)
        verified_depths = list(list_depth_facts(self.turns, payload['lesson']).values())[:12]
        self.has_visual_material = payload['lesson'].get('has_visual_material', False)
        teachers = {s['id'] for s in classroom['teacher_segments']}
        final_prompt = REFLECTION_REPORT.replace('页面不列六项评分。', '') + (
            '\n最终JSON必须包含assessment对象（每个条件只填证据，数值由程序出）。'
            'overview始终是含text和evidence的对象。若某项缺乏依据，其assessment维度返回空对象。'
            '已有例题、实际小纸已经给出正确理由、教师随后补充规则时，'
            '把“先后怎么安排”“换题前多说一句”“由教师再演示”等建议标为可选提升。'
            '尚未看到更完整的讲法，不能证明已有讲解妨碍理解。'
        ) + RUBRIC_PROMPT + ACCURACY_PROMPT
        has_typed_input = any(t['role'] == 'teacher' and t.get('mode') == 'text' for t in self.turns)
        filler_prompt = TYPED_FILLER_COUNTS_PROMPT if has_typed_input else FILLER_COUNTS_PROMPT
        if has_typed_input:
            final_prompt = final_prompt.replace('键盘文字、代码和AI回应都不能当口头禅。', '')
        if not any(t.get('mode') == 'microphone' for t in self.turns):
            final_prompt += ('\n本次只有打字讲授，顶层expression（语音表达观察正文）必须是null。'
                             'assessment.expression仍按评分规则检查教师文字讲解。')
        if len(encode(payload)) <= SHORT_LIMIT:
            self.progress('正在分析讲授内容与改进建议……')
            if verified_depths:
                payload['verified_list_depths'] = verified_depths
            report = await self.stage('teaching_report', final_prompt + (
                '\nassessment.accuracy列出逐条核对结果及逐字引文。没有可核对的知识说法时返回[]。'
                '每条basis控制在一两句话，次要说法只保留有诊断价值的。核心核对因篇幅受限时，'
                '设置accuracy_coverage_limited为true。'
            ) + filler_prompt, payload, ScoredReflectionReport, teachers)
            if report.assessment.accuracy is None:
                report.assessment.accuracy = []
                report.accuracy_coverage_limited = True
            report.word_counts = rank_filler_counts(count_fillers(report.word_counts, payload['turns']))
            return report
        parts = list(chunks(payload))
        groups, claims, accuracy_limited = [], [], False
        counts = Counter()
        for i, part in enumerate(parts):
            self.progress(f'正在分析长课内容 {i + 1}/{len(parts)}……')
            allowed = {t['id'] for t in part['turns'] if t['role'] == 'teacher'}
            notes = await self.stage('teaching_report_chunk', ANALYZE_CHUNK + REFLECTION_NOTES + (
                '\n另在accuracy_claims记录本段教师实际讲授的核心知识主张，以及有诊断价值的次要说法。'
                '逐条列claim、core、evidence（原始source_id、角色和逐字短引）。用连续原句，不能改字或加标点。'
                '同一句在同一source_id中反复出现时，证据须带相邻的例号、数值或更正语句，'
                '使这段连续原文在该来源中只出现一次；不要单引每个例子都会重复的结论。'
                '保留后续更正、限定条件及讲法不同的结论。本段重复且意思相同的只记一次，'
                '小纸作答仅在教师随后明确采用时保留，来源用输入中的reply_id。没有适用说法时返回[]。'
            ) + filler_prompt, part, AccuracyChunkNotes, allowed, reflection=True)
            groups.append({'range': [i, i], **notes.model_dump(include={'notes'})})
            found, limited = chunk_accuracy_claims(part, notes.accuracy_claims)
            claims.extend(found)
            accuracy_limited |= limited
            if counts is not None:
                segment_counts = count_fillers(notes.word_counts, part['turns'])
                if segment_counts is None:
                    counts = None
                else:
                    counts.update(segment_counts)
        # Coverage is tracked by the program, rather than trusting a model's claim.
        level = 0
        while len(encode({'lesson': payload['lesson'], 'groups': groups,
                          'interaction_facts': payload['interaction_facts']})) > SHORT_LIMIT:
            level += 1
            merged = []
            for i, batch in enumerate(batches(groups)):
                self.progress(f'正在整理全课前后关系（第{level}轮）……')
                merge_payload = {'groups': batch}
                notes = await self.stage('teaching_report_merge', MERGE_NOTES + REFLECTION_NOTES, merge_payload,
                                         AnalysisNotes, note_sources(merge_payload))
                merged.append({'range': [batch[0]['range'][0], batch[-1]['range'][1]], **notes.model_dump()})
            if len(encode(merged)) >= len(encode(groups)):
                raise ModelError('长课笔记未能有效压缩；成功阶段已保留，可重试继续，未截断课堂')
            groups = merged
        assert groups[0]['range'][0] == 0 and groups[-1]['range'][1] == len(parts) - 1
        self.progress('正在核对全课解释、更正与优先改进……')
        final_payload = {'lesson': payload['lesson'], 'groups': groups,
                         'interaction_facts': payload['interaction_facts']}
        allowed = note_sources(final_payload)
        candidates, limited = accuracy_context(claims, self.turns)
        allowed.update(item['source_id'] for claim in candidates for item in claim['evidence']
                       if item['actor'] == 'teacher')
        final_payload.update(accuracy_claims=candidates, teacher_excerpts=teacher_excerpts(self.turns, allowed))
        if verified_depths:
            final_payload['verified_list_depths'] = verified_depths
        report = await self.stage('teaching_report', final_prompt + (
            '\n长课的groups内是总结，引用时从teacher_excerpts和accuracy_claims选逐字原句，核对编号。'
            'assessment.accuracy用claim_id指回accuracy_claims中的id，再写topic、verdict和简短basis。'
            '原句、角色、核心范围由程序填回，不要重写。逐条返回判定，即使结论待核实。'
            '明确更正时保留前后各自的判定，更正项填corrects_earlier。'
        ), final_payload, LongScoredReflectionReport, allowed)
        result = bind_accuracy_result(report, candidates, accuracy_limited or limited)
        result.word_counts = rank_filler_counts(counts)
        return result
