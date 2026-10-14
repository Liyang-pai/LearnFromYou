"""Identity of the code loaded by this process, frozen once at startup."""
import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAPABILITY = 'question_release_v1'


def fingerprint(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _identity():
    digest = hashlib.sha256()
    for name in ('schemas.py', 'prompts.py', 'policy.py', 'session.py', 'runtime.py'):
        digest.update(name.encode())
        digest.update((ROOT / 'backend' / name).read_bytes())
    try:
        commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True,
                                text=True, timeout=2, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = None
    return {'code_root': str(ROOT), 'pid': os.getpid(), 'started_at': datetime.now(timezone.utc).isoformat(),
            'commit': commit, 'rules_fingerprint': digest.hexdigest(), 'capabilities': [CAPABILITY]}


RUNTIME = _identity()
