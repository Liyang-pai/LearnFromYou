"""Recompute depth and pair counts for explicit generalized-list literals."""
import json
import re
from unicodedata import normalize


_ASSIGNMENT = re.compile(r'(?<![A-Za-z0-9_])([A-Z][A-Za-z0-9_]*)\s*=\s*(\([A-Za-z0-9_(), \t]{0,180})')
_DIGITS = dict(zip('零一二三四五六七八九十', range(11))) | {'两': 2}
_NUMBER = re.compile(r'深度\s*(?:就是|是|应为|为|等于|=)\s*(\d{1,4}|[零一二三四五六七八九十两])(?![\d零一二三四五六七八九十两.])')
_COUNTS_PAIRS = re.compile(r'深度\s*(?:就是|是|为|等于)\s*括号的?(?:总对数|对数|个数)')
_CLAUSE_END = re.compile(r'[,，。！？!?；;\n]')
_SENTENCE_END = re.compile(r'[。！？!?；;\n]')
_NAMED_SUBJECT = re.compile(r'(?<![A-Za-z0-9_])([A-Z][A-Za-z0-9_]*)\s*的?\s*$')
_EXAMPLE_POINTER = r'(?:这个|该|此)(?:式子|广义表|表)'
_DEPTH_SUBJECT = re.compile(rf'(?:(?:所以|因此|故|那么|可见|则)\s*)?(?:(?:{_EXAMPLE_POINTER}|它|其)\s*)?(?:的\s*)?')
_OTHER_EXAMPLE = re.compile(
    r'另(?:一|外)|(?:下一|换(?:一)?)(?:个|道|题)|再(?:来)?看|空(?:广义)?表|'
    r'第\s*[一二三四五六七八九十两0-9]+\s*(?:道题|题|个例子|例)|\([A-Za-z0-9_(), \t]*\)'
)


def list_depth_facts(turns, lesson):
    if not lesson or '广义表' not in str(lesson.get('topic', '')) + str(lesson.get('points', '')):
        return {}
    facts = {}
    for turn in turns:
        if turn['role'] != 'teacher':
            continue
        matches = _ASSIGNMENT.findall(normalize('NFKC', turn['text']))
        if len(matches) != 1:
            continue
        name, tail = matches[0]
        depth = maximum = pairs = 0
        for index, char in enumerate(tail):
            if char.isupper():
                break  # Named sublists can change depth and require additional context.
            if char == '(':
                depth += 1
                pairs += 1
                maximum = max(maximum, depth)
            elif char == ')':
                depth -= 1
                if depth == 0:
                    literal = tail[:index + 1]
                    if tail[index + 1:].lstrip().startswith(('(', ')')):
                        break
                    tokens = re.sub(r'[a-z0-9_]+', r'"\g<0>"', literal)
                    try:
                        json.loads(tokens.replace('(', '[').replace(')', ']'))
                    except (ValueError, RecursionError):
                        break
                    facts[turn['id']] = {'source_id': turn['id'], 'expression': name + '=' + literal,
                                         'pairs': pairs, 'max_depth': maximum}
                    break
    return facts


def _example_for_depth(prefix, index, turns, facts):
    following = []
    for earlier in range(index, max(-1, index - 3), -1):
        turn = turns[earlier]
        text = prefix if earlier == index else normalize('NFKC', turn['text'])
        assignment = _ASSIGNMENT.search(text)
        if assignment is None:
            following.append(text)
            continue
        fact = facts.get(turn['id'])
        if fact is None:
            return None
        name, literal = fact['expression'].split('=', 1)
        start = assignment.start(2)
        if not text.startswith(literal, start):
            return None

        context = '。'.join((text[start + len(literal):], *reversed(following)))
        subject = _CLAUSE_END.split(context)[-1].strip(' `\t$')
        named = _NAMED_SUBJECT.search(subject)
        if named:
            return fact if named[1] == name else None
        if not _DEPTH_SUBJECT.fullmatch(subject) or _OTHER_EXAMPLE.search(context):
            return None
        if re.search(_EXAMPLE_POINTER, _SENTENCE_END.split(prefix)[-1]):
            return fact
        if earlier == index and _DEPTH_SUBJECT.fullmatch(context.strip(' ,，。；;`\t$')):
            return fact
        return None
    return None


def check_list_depth(quote, position, turns, facts):
    quote = normalize('NFKC', quote)
    statements = list(_NUMBER.finditer(quote))
    values = {statement[1] for statement in statements}
    if len(values) != 1 or re.search(r'\?|吗|刚才说|之前说', quote):
        return None
    index, offset = position
    prefix = normalize('NFKC', turns[index]['text'][:offset])
    examples = [_example_for_depth(prefix + quote[:statement.start()], index, turns, facts)
                for statement in statements]
    fact = examples[0]
    if fact is None or any(example != fact for example in examples[1:]):
        return None

    (value,) = values
    declared = _DIGITS[value] if value in _DIGITS else int(value)
    depth = fact['max_depth']
    calculation = f"程序复算：{fact['expression']} 有{fact['pairs']}对括号，最大嵌套为{depth}层。"
    if declared == depth:
        return 'correct', calculation
    verdict = 'core_error' if _COUNTS_PAIRS.search(quote) else 'key_error'
    return verdict, calculation + f'原话报出的深度为{declared}。'
