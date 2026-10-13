"""Offline bootstrap checks; never install packages or call an LLM."""
from importlib import metadata
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import re
from threading import Thread
from types import SimpleNamespace

import pytest

from scripts import bootstrap


@pytest.mark.parametrize('fails', [False, True])
def test_entry_point_outputs_chinese_with_cp1252_streams(fails):
    # Reproduce Windows CI's redirected encoding on every platform, without
    # creating a venv or downloading dependencies.
    code = '''
import sys
from types import SimpleNamespace
from scripts import bootstrap
sys.argv = ['bootstrap.py', '--prepare-only']
bootstrap.supported_python = lambda: True
bootstrap.subprocess.run = lambda *a, **kw: SimpleNamespace(returncode=0)
def prepare(root):
    bootstrap.run_with_activity(['example'], '正在创建虚拟环境……', root)
    if FAILS:
        raise OSError('中文错误')
    return sys.executable
bootstrap.prepare_venv = prepare
bootstrap.prepare_dependencies = lambda *a: print('依赖完整，无需下载安装。')
sys.exit(bootstrap.main())
'''.replace('FAILS', repr(fails))
    result = subprocess.run([sys.executable, '-c', code], cwd=bootstrap.ROOT,
                            env={**os.environ, 'PYTHONIOENCODING': 'cp1252:strict'},
                            capture_output=True, timeout=15)
    stdout = result.stdout.decode('utf-8')
    stderr = result.stderr.decode('utf-8')
    assert '正在创建虚拟环境……' in stdout
    assert result.returncode == int(fails), stderr
    if fails:
        assert '启动失败：中文错误' in stderr
    else:
        assert '依赖完整，无需下载安装。' in stdout


@pytest.mark.parametrize('version,expected', [((3, 10, 9), False), ((3, 11, 0), True),
                                            ((3, 12, 0), True), ((3, 13, 0), True), ((3, 14, 0), False)])
def test_supported_versions(monkeypatch, version, expected):
    monkeypatch.setattr(sys, 'version_info', version)
    assert bootstrap.supported_python() is expected


def test_rejects_free_threaded_and_wrong_venv(monkeypatch, tmp_path):
    assert not bootstrap.supported_python(tmp_path / 'elsewhere')
    monkeypatch.setattr(bootstrap.sysconfig, 'get_config_var', lambda name: 1)
    assert not bootstrap.supported_python()


@pytest.mark.parametrize('machine,bits,expected', [('AMD64', 8, True), ('AMD64', 4, False),
                                                ('ARM64', 8, False)])
def test_windows_requires_x64(monkeypatch, machine, bits, expected):
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(bootstrap.platform, 'machine', lambda: machine)
    monkeypatch.setattr(bootstrap.struct, 'calcsize', lambda _: bits)
    assert bootstrap.supported_python() is expected


def test_creates_real_venv_in_chinese_space_path_and_reuses_it(tmp_path, monkeypatch):
    root = tmp_path / '中文项目 with spaces'
    root.mkdir()
    executable = bootstrap.prepare_venv(root)
    assert executable.is_file()
    assert bootstrap.valid_venv(root)
    monkeypatch.setattr(bootstrap, 'checked', lambda *args: pytest.fail('must reuse complete venv'))
    assert bootstrap.prepare_venv(root) == executable


def test_wrong_or_broken_venv_is_backed_up_before_rebuild(tmp_path, monkeypatch):
    original = tmp_path / '.venv'
    original.mkdir()
    (original / 'keep.txt').write_text('keep old environment', encoding='utf-8')
    statuses = iter([False, True])
    monkeypatch.setattr(bootstrap, 'valid_venv', lambda _: next(statuses))

    def create(command, stage, root):
        assert not original.exists()
        assert command[-1] == str(original)
        original.mkdir()

    monkeypatch.setattr(bootstrap, 'checked', create)
    bootstrap.prepare_venv(tmp_path)
    backups = list(tmp_path.glob('.venv.backup-*'))
    assert len(backups) == 1
    assert (backups[0] / 'keep.txt').read_text() == 'keep old environment'


def test_broken_executable_probe_is_not_accepted(tmp_path, monkeypatch):
    executable = bootstrap.venv_python(tmp_path)
    executable.parent.mkdir(parents=True)
    executable.touch()

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 15)

    monkeypatch.setattr(bootstrap.subprocess, 'run', timeout)
    assert not bootstrap.valid_venv(tmp_path)


@pytest.mark.parametrize('installed', [None, '0.1'])
def test_missing_and_out_of_range_packages_need_repair(tmp_path, monkeypatch, installed):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('example-package>=2,<3\n', encoding='utf-8')

    def version(name):
        if installed is None:
            raise metadata.PackageNotFoundError(name)
        return installed

    monkeypatch.setattr(metadata, 'version', version)
    assert bootstrap.check_dependencies(requirements) == 1


def test_transitive_conflicts_and_native_import_errors_need_repair(tmp_path, monkeypatch):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('', encoding='utf-8')
    monkeypatch.setattr(bootstrap.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=1, stdout='conflicting packages', stderr=''))
    assert bootstrap.check_dependencies(requirements) == 2
    monkeypatch.setattr(bootstrap.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout='', stderr=''))

    def broken_native_module(name):
        raise OSError('DLL unavailable')

    monkeypatch.setattr(bootstrap.importlib, 'import_module', broken_native_module)
    assert bootstrap.check_dependencies(requirements) == 2


def test_complete_environment_never_invokes_install_or_ensurepip(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(bootstrap.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(bootstrap, 'checked', lambda command, *a: calls.append(command))
    bootstrap.prepare_dependencies(Path(sys.executable), tmp_path)
    assert len(calls) == 1  # only the app import check
    assert '-c' in calls[0]
    assert not any('install' in command or 'ensurepip' in command for command in calls)


def test_missing_pip_and_dependencies_are_restored_then_verified(tmp_path, monkeypatch):
    statuses = iter([1, 1])  # pip missing, dependency check fails
    calls = []
    monkeypatch.setattr(bootstrap.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=next(statuses)))
    monkeypatch.setattr(bootstrap, 'checked', lambda command, *a: calls.append(command))
    bootstrap.prepare_dependencies(Path(sys.executable), tmp_path)
    assert 'ensurepip' in calls[0]
    assert 'install' in calls[1] and str(tmp_path / 'requirements.txt') in calls[1]
    assert '--check-dependencies' in calls[2]
    assert '-c' in calls[3]


def test_install_failure_stops_before_app_launch(tmp_path, monkeypatch):
    statuses = iter([0, 1])  # pip works, a dependency is missing
    monkeypatch.setattr(bootstrap.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=next(statuses)))

    def failed_install(command, *args):
        assert 'install' in command
        raise RuntimeError('offline')

    monkeypatch.setattr(bootstrap, 'checked', failed_install)
    with pytest.raises(RuntimeError, match='offline'):
        bootstrap.prepare_dependencies(Path(sys.executable), tmp_path)


def test_intact_metadata_but_broken_packages_are_reinstalled(tmp_path, monkeypatch):
    statuses = iter([0, 2])
    calls = []
    monkeypatch.setattr(bootstrap.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=next(statuses)))
    monkeypatch.setattr(bootstrap, 'checked', lambda command, *a: calls.append(command))
    bootstrap.prepare_dependencies(Path(sys.executable), tmp_path)
    assert '--force-reinstall' in calls[0]
    assert '--check-dependencies' in calls[1]


def test_failed_command_includes_stage_and_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=7))
    with pytest.raises(RuntimeError, match='安装依赖失败（退出码 7）'):
        bootstrap.checked(['example'], '安装依赖', tmp_path)


def test_interactive_activity_keeps_moving_and_preserves_errors(tmp_path, monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    terminal = Terminal()
    monkeypatch.setattr(sys, 'stdout', terminal)
    command = [sys.executable, '-X', 'utf8', '-c',
               'import time,sys; time.sleep(.7); print("下载完成", flush=True); '
               'time.sleep(.5); print("安装失败：测试错误", file=sys.stderr); sys.exit(7)']
    with pytest.raises(RuntimeError, match='退出码 7'):
        bootstrap.checked(command, '正在安装依赖', tmp_path)
    output = terminal.getvalue()
    assert '下载完成' in output and '安装失败：测试错误' in output
    assert len(set(re.findall(r'\[([|/\\-])\]', output))) >= 2
    assert '已等待' in output
    assert '\x1b' not in output  # compatible with the classic Windows console


def test_redirected_output_uses_plain_logs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    assert bootstrap.run_with_activity(['example'], '正在配置环境', tmp_path) == 0
    assert capsys.readouterr().out == '正在配置环境\n'


@pytest.mark.skipif(os.name != 'nt', reason='requires Windows PowerShell')
def test_windows_detection_and_installer_failure_paths():
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                             str(bootstrap.ROOT / 'tests/test_windows_bootstrap.ps1')],
                            capture_output=True, timeout=45, encoding='utf-8', errors='replace')
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name != 'nt', reason='requires Windows PowerShell HttpClient')
@pytest.mark.parametrize('mode', ['known', 'unknown', 'truncated'])
def test_windows_streaming_download_and_progress(tmp_path, mode):
    payload = bytes(range(256)) * 900

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if mode != 'unknown':
                self.send_header('Content-Length', str(len(payload) + (99 if mode == 'truncated' else 0)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        env = {**os.environ,
               'LFY_DOWNLOAD_LAUNCHER': str(bootstrap.ROOT / 'scripts/start_windows.ps1'),
               'LFY_DOWNLOAD_URL': f'http://127.0.0.1:{server.server_port}/installer',
               'LFY_DOWNLOAD_FILE': str(tmp_path / '下载 with spaces.exe'),
               'LFY_DOWNLOAD_PROGRESS': str(tmp_path / 'progress.json')}
        command = '''. $env:LFY_DOWNLOAD_LAUNCHER
$script:Percents = @()
function Write-Progress {
    param($Id, $Activity, $Status, $PercentComplete, [switch]$Completed)
    if (-not $Completed) { $script:Percents += $PercentComplete }
}
$result = 0
try { Receive-InstallerDownload -Uri $env:LFY_DOWNLOAD_URL -Destination $env:LFY_DOWNLOAD_FILE }
catch { Write-Host $_.Exception.Message; $result = 1 }
finally {
    ConvertTo-Json -InputObject @($script:Percents) -Compress | Set-Content -Encoding UTF8 $env:LFY_DOWNLOAD_PROGRESS
}
exit $result
'''
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                     '-Command', command], env=env, capture_output=True,
                                    timeout=25, encoding='utf-8', errors='replace')
        finally:
            server.shutdown()
            worker.join(timeout=2)
    progress = json.loads((tmp_path / 'progress.json').read_text(encoding='utf-8-sig'))
    if mode == 'truncated':
        assert result.returncode != 0, result.stdout + result.stderr
        assert 100 not in progress
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert (tmp_path / '下载 with spaces.exe').read_bytes() == payload
        assert progress[-1] == 100
        if mode == 'known':
            # Content-Length becomes known only after response headers arrive.
            # Connecting may report indeterminate progress even for this URL.
            determinate = [percent for percent in progress[:-1] if percent != -1]
            assert determinate, progress
            assert all(0 <= percent <= 99 for percent in determinate), progress
            first_known = next(index for index, percent in enumerate(progress) if percent != -1)
            assert -1 not in progress[first_known:], progress
        else:
            assert -1 in progress
