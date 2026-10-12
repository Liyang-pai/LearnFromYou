"""Offline bootstrap checks; never install packages or call an LLM."""
from importlib import metadata
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import bootstrap


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


@pytest.mark.skipif(os.name != 'nt', reason='requires Windows PowerShell')
def test_windows_detection_and_installer_failure_paths():
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                             str(bootstrap.ROOT / 'tests/test_windows_bootstrap.ps1')],
                            capture_output=True, timeout=45, encoding='utf-8', errors='replace')
    assert result.returncode == 0, result.stdout + result.stderr
