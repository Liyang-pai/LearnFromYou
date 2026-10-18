"""Replay existing real classroom logs locally. Never invokes a model or changes originals."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config
from backend.history import HistoryService


def expected_from_log(path):
    batches, originals, seen, replies = [], {}, set(), []
    for line in path.open(encoding='utf-8-sig'):
        try:
            item = json.loads(line); data = item.get('data', {})
            if item.get('type') == 'transcript':
                originals[data['id']] = data['text']
            elif item.get('type') == 'reply' and data.get('kind') in ('question', 'answer', 'ack') and data.get('text', '').strip():
                replies.append(data['text'])
            elif item.get('type') == 'llm_request' and data.get('phase') == 'student':
                for message in reversed(data.get('body', {}).get('messages', [])):
                    if message.get('role') != 'user': continue
                    content = message.get('content')
                    if isinstance(content, list): content = next((part.get('text') for part in content if part.get('type') == 'text'), '')
                    try: payload = json.loads(content)
                    except (ValueError, TypeError): continue
                    if not isinstance(payload, dict) or 'new_teacher_segments' not in payload: continue
                    segments = payload['new_teacher_segments']
                    if segments:
                        signature = tuple((source['id'], source['text']) for source in segments)
                        if signature not in seen:
                            seen.add(signature)
                            batches.append({'source_ids': [source['id'] for source in segments],
                                'text': '\n'.join(source['text'] for source in segments), 'time': item.get('time')})
                    break
        except (ValueError, KeyError, TypeError): continue
    return batches, originals, replies


async def main(args):
    results = []
    for sid in args.sessions:
        source = args.source_dir / f'{sid}.jsonl'
        if not source.is_file(): raise FileNotFoundError(f'需要已有真实课堂日志：{source}')
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        expected, originals, replies = expected_from_log(source)
        with tempfile.TemporaryDirectory(prefix='lfy-batch-replay-') as folder:
            config.ROOT = Path(folder); (config.ROOT / 'logs').mkdir()
            path = config.ROOT / 'logs' / source.name; shutil.copyfile(source, path)
            service = HistoryService(); start = time.perf_counter()
            await service.detail(sid); await service.jobs[sid]
            rows, before = [], None
            while True:
                page = await service.dialogue(sid, before)
                assert len(page['items']) <= 50
                rows = page['items'] + rows
                before = page['before']
                if before is None: break
            elapsed = (time.perf_counter() - start) * 1000
            batches = [row for row in rows if row.get('request_count')]
            assert [{key: row[key] for key in ('source_ids', 'text', 'time')} for row in batches] == expected
            assert [row['text'] for row in rows if row['role'] == 'student'] == replies
            covered = {sid for row in rows if row['role'] == 'teacher' for sid in row['source_ids']}
            assert set(originals) <= covered
            for batch in batches:
                assert len(batch['segments']) == len(batch['source_ids'])
                for part in batch['segments']:
                    assert part['raw_text'] == originals.get(part['source_id'])
            original_open = Path.open
            def guard(self, *a, **kw):
                if self == path: raise AssertionError('cache hit reread the raw JSONL')
                return original_open(self, *a, **kw)
            restored = HistoryService()
            with patch.object(Path, 'open', guard):
                start = time.perf_counter()
                await restored.listing(); await restored.detail(sid); await restored.dialogue(sid)
                warm = (time.perf_counter() - start) * 1000
            target = next((row for row in batches if 't110' in row['source_ids']), None)
            if sid == '95881d605634':
                assert target and len(target['segments']) == 54
                assert sum(len(part['text']) for part in target['segments']) == 590
                if args.browser_data:
                    logs = args.browser_data / 'logs'; logs.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, logs / source.name)
                    (args.browser_data / 'replay-expected.json').write_text(json.dumps({
                        'text': target['text'], 'raw': [part['raw_text'] for part in target['segments']]}, ensure_ascii=False))
            results.append({'session_id': sid, 'raw_bytes': source.stat().st_size, 'original_fragments': len(originals),
                'submitted_batches': len(batches), 'student_replies': len(replies), 'display_rows': len(rows),
                'unconfirmed_fragments': sum(row.get('processing_status') == 'unconfirmed' for row in rows),
                'cold_parse_and_all_pages_ms': round(elapsed, 2), 'warm_list_detail_page_ms': round(warm, 2),
                'warm_raw_reads': 0, 'target_fragments': len(target['segments']) if target else None,
                'target_characters': sum(len(part['text']) for part in target['segments']) if target else None})
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(args.output.read_text())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-dir', type=Path, default=Path('logs'))
    parser.add_argument('--sessions', nargs='+', default=['95881d605634', '6a06f0778766'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--browser-data', type=Path)
    asyncio.run(main(parser.parse_args()))
