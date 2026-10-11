"""Exercise the Windows double-click path without browsers, inference or paid calls."""
import os
from pathlib import Path
import socket
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_windows_batch_has_no_multibyte_commands_or_lf_only_lines():
    content = (ROOT / '启动试讲.bat').read_bytes()
    content.decode('ascii')
    assert b'\r\n' in content and b'\n' not in content.replace(b'\r\n', b'')


@pytest.mark.skipif(os.name != 'nt', reason='Windows batch launcher')
@pytest.mark.parametrize('occupied,launch', [(False, 'cmd'), (True, 'cmd'), (False, 'shell')])
def test_batch_opens_browser_only_after_its_own_server_is_ready(tmp_path, occupied, launch):
    if not (ROOT / '.venv/Scripts/python.exe').is_file():
        pytest.skip('requires configured local interpreter')
    marker = tmp_path / 'browser-opened.txt'
    hook = tmp_path / 'sitecustomize.py'
    hook.write_text('''import json
from pathlib import Path
from threading import Timer
from urllib.request import urlopen
import uvicorn
import webbrowser
original = uvicorn.Server.run
def bounded_run(server, *args, **kwargs):
    timer = Timer(3, lambda: setattr(server, 'should_exit', True))
    timer.daemon = True
    timer.start()
    try:
        return original(server, *args, **kwargs)
    finally:
        timer.cancel()
def verify_ready(url):
    with urlopen(url + '/api/health', timeout=2) as response:
        state = json.load(response)
    with urlopen(url + '/static/teaching-report.js', timeout=2) as response:
        script = response.read().decode('utf-8')
    assert 'asr_ready' in state and state['model_configured']
    assert '师生互动' in script
    Path(''' + repr(str(marker)) + ''').write_text(url, encoding='utf-8')
    return True
uvicorn.Server.run = bounded_run
webbrowser.open = verify_ready
''', encoding='utf-8')
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
        if occupied:
            reservation.listen()
        else:
            reservation.close()
        env = {**os.environ, 'PORT': str(port), 'PYTHONPATH': str(tmp_path),
               'PYTHONUTF8': '1', 'DEEPSEEK_API_KEY': 'offline-test',
               'LFY_LAUNCHER_BAT': str(ROOT / '启动试讲.bat'), 'LFY_LAUNCHER_CWD': str(tmp_path)}
        command = ['cmd.exe', '/c', str(ROOT / '启动试讲.bat')]
        if launch == 'shell':
            # Invoke the Windows .bat file association, as Explorer does, with
            # a hidden test window and a real foreground Python child.
            command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                '$taskLaunched = Start-Process -FilePath $env:LFY_LAUNCHER_BAT '
                '-WorkingDirectory $env:LFY_LAUNCHER_CWD -WindowStyle Hidden -PassThru; '
                'if (-not $taskLaunched.WaitForExit(15000)) { '
                'Stop-Process -Id $taskLaunched.Id; throw "Test launcher did not exit" }; '
                'exit $taskLaunched.ExitCode']
        process = subprocess.run(command, cwd=tmp_path,
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=20, encoding='utf-8', errors='replace')
    if occupied:
        assert process.returncode != 0, process.stdout
        assert not marker.exists(), 'must not open a different service on the occupied port'
    else:
        assert process.returncode == 0, process.stdout
        assert marker.read_text(encoding='utf-8') == f'http://127.0.0.1:{port}'
    assert '系统找不到指定的路径' not in process.stdout
    # The automatically stopped test service must leave no listener behind.
    with socket.socket() as probe:
        probe.settimeout(0.2)
        assert probe.connect_ex(('127.0.0.1', port)) != 0
