"""离线检查真实画像证据，不调用模型；内容筛查只定位复核项，不冒充语义验收。"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re


HEADERS = ('已有知识', '理解基础', '知识边界', '可能的理解困难')
GRADES = {'一年级', '二年级', '三年级', '四年级', '五年级', '六年级',
          '七年级', '八年级', '九年级', '高一', '高二', '高三', '大一', '大二', '大三', '大四'}


def sections(text):
    matches = list(re.finditer(r'(?m)^(已有知识|理解基础|知识边界|可能的理解困难)[：:]?', text))
    return {m[1]: text[m.end():matches[i+1].start() if i+1 < len(matches) else len(text)].strip()
            for i, m in enumerate(matches)}


def audit(paths):
    failures, flags, records = [], [], []
    versions = set()
    for path in paths:
        data = json.loads(path.read_text())
        versions.add(data['prompts_sha256'])
        for case in data['cases']:
            key = str(path.parent) + '/' + case['id']
            if case['status'] != 'completed':
                failures.append([key, case.get('error', case['status'])])
                continue
            settings, result = case['settings'], case['profile']
            parts = sections(result['student_profile'])
            if tuple(parts) != HEADERS:
                failures.append([key, 'four sections missing or out of order'])
                continue
            for field in ('grade', 'subject', 'points'):
                if settings[field].strip() and result[field] != settings[field]:
                    failures.append([key, 'teacher field changed: ' + field])
            if not settings['grade'] and result['grade'] not in GRADES:
                flags.append([key, 'check inferred custom grade', result['grade']])
            known = parts['已有知识'].splitlines()
            actual = case.get('preparation', {}).get('initial_prerequisites')
            if actual is not None and Counter(known) != Counter(actual):
                failures.append([key, 'initial knowledge coverage mismatch', known, actual])
            if settings['student_level'] == 'strong' and sum('能独立' in line for line in known) < 2:
                failures.append([key, 'strong lacks two concrete independent prerequisite tasks'])
            if settings['student_level'] == 'weak':
                gaps = re.split(r'[；。\n]', parts['可能的理解困难'])
                if sum('不稳固' in gap for gap in gaps) < 2:
                    flags.append([key, 'review whether weak has two concrete prerequisite gaps'])
            if settings['student_level'] == 'normal' and not ('独立' in parts['理解基础'] and '示例' in parts['理解基础']):
                failures.append([key, 'normal prerequisite independence/support mismatch'])
            if settings['lesson_type'] != 'new':
                if '确认' not in parts['理解基础'] or '确认' not in parts['知识边界']:
                    failures.append([key, 'review/application invents target mastery'])
                if re.search(r'尚未学习|已经掌握|熟练掌握', parts['理解基础'] + parts['知识边界']):
                    flags.append([key, 'check unsupported learning history'])
            if settings['grade'] in ('一年级', '小学一年级', '二年级'):
                if re.search(r'有理数|代数|坐标|比例|电流|电压|电阻|电路|正负极', parts['已有知识']):
                    failures.append([key, 'low grade has advanced prerequisites'])
            prefix = case['id'][:2]
            rules = {
                '01': r'等式两边同时|判断.{0,12}方程.{0,5}解|检验|移项|能.{0,8}解方程',
                '02': r'(?<!正比例函数 )y\s*[=＝]\s*kx\s*[+＋]|常数项|截距|平移',
                '03': r'密度|ρ|质量.{0,10}体积.{0,10}(正比|比值|一定)',
                '04': r'I\s*[=＝]\s*U|电流.{0,15}电压.{0,10}(正比|反比)|控制变量',
                '05': r'反应前后|质量守恒|原子|气体.{0,8}(进入|逸出).{0,12}(增|减)',
            }
            for line in known:
                if re.search(rules.get(prefix, r'(?!)'), line):
                    flags.append([key, 'possible target leakage', line])
            if prefix == '04' and not all(word in parts['已有知识'] for word in ('正比例', '比值', '反比例', '乘积')):
                failures.append([key, 'missing mathematical proportion definitions'])
            if prefix == '05' and not all(word in parts['已有知识'] for word in ('气体', '质量')):
                failures.append([key, 'missing gas mass prerequisite'])
            records.append({'case': key, 'known_lines': len(known), 'extraction_checked': actual is not None,
                            'grade': result['grade'], 'level': settings['student_level'], 'lesson_type': settings['lesson_type']})
    return {'status': 'checks_passed_semantic_review_required' if not failures else 'failed',
            'prompt_versions': sorted(versions), 'same_prompt_version': len(versions) == 1,
            'version_note': '不同版本只作逐项证据，不能合并声称同版本整体验收通过。',
            'cases': len(records), 'known_lines': sum(r['known_lines'] for r in records),
            'failures': failures, 'semantic_flags': flags, 'records': records}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('summaries', nargs='+', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = audit(args.summaries)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text)
    print(text)
    raise SystemExit(bool(result['failures']))
