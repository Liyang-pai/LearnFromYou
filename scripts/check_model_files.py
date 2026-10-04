"""Fail CI if any file under asr/models/ is committed, regardless of size."""
import subprocess
import sys


def tracked_models():
    result = subprocess.run(['git', 'ls-files', '-z', '--', 'asr/models/'],
                            check=True, capture_output=True)
    return [p.decode('utf-8') for p in result.stdout.split(b'\0') if p]


if __name__ == '__main__':
    paths = tracked_models()
    if paths:
        print('模型目录不能提交到 Git（包括小型 VAD、词表和本地配置）：', file=sys.stderr)
        print('\n'.join(paths), file=sys.stderr)
        sys.exit(1)
    print('PASS: no tracked files under asr/models/')
