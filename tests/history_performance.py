"""Non-paid acceptance for large raw logs and event-loop responsiveness."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config
from backend.history import HistoryService
from tests.test_history import seed, SID


async def main(output):
    results = []
    with tempfile.TemporaryDirectory(prefix='lfy-history-perf-') as folder:
        config.ROOT = Path(folder)
        for megabytes in (30, 60):
            path = seed(config.ROOT, count=121)
            filler = json.dumps({'type': 'llm_request', 'data': {'phase': 'student', 'body': {'messages': [
                {'role': 'user', 'content': '重复的完整模型上下文，仅供诊断。' * 5000}]}}}, ensure_ascii=False).encode() + b'\n'
            with path.open('ab') as stream:
                stream.write(b'\n')
                while stream.tell() < megabytes * 1024 * 1024: stream.write(filler)
            service = HistoryService(); heartbeats = []; done = False
            async def heartbeat():
                while not done:
                    before = time.perf_counter(); await asyncio.sleep(.005)
                    heartbeats.append((time.perf_counter() - before) * 1000)
            beat = asyncio.create_task(heartbeat()); start = time.perf_counter()
            result = await service.detail(SID)
            assert result['status'] == 'indexing'
            await service.jobs[SID]; await service.detail(SID)
            cold = (time.perf_counter() - start) * 1000
            done = True; await beat
            original = Path.open
            def guarded(self, *args, **kwargs):
                if self == path: raise AssertionError('warm request scanned raw log')
                return original(self, *args, **kwargs)
            # A process restart must also reuse the archive without scanning JSONL.
            restored = HistoryService()
            with patch.object(Path, 'open', guarded):
                start = time.perf_counter()
                listing = await restored.listing(); detail = await restored.detail(SID)
                page = await restored.dialogue(SID)
                warm = (time.perf_counter() - start) * 1000
            assert listing['total'] == 1 and detail['turn_count'] == 162 and len(page['items']) == 50
            results.append({'raw_bytes': path.stat().st_size, 'cold_ms': round(cold, 2),
                'warm_list_detail_page_ms': round(warm, 2), 'max_heartbeat_interval_ms': round(max(heartbeats), 2),
                'dialogue_payload_bytes': len(json.dumps(page, ensure_ascii=False).encode()),
                'warm_raw_reads': 0})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2)); print(output.read_text())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    asyncio.run(main(parser.parse_args().output))
