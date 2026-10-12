"""Prepare the local environment; this entry point initially needs only stdlib."""
import argparse
from datetime import datetime
from importlib import metadata
import importlib
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('fastapi', 'uvicorn', 'httpx', 'anyio', 'dotenv', 'websockets',
           'pydantic', 'numpy', 'sherpa_onnx', 'soundfile', 'packaging')


def supported_python(expected_venv=None):
    if (sys.implementation.name != 'cpython' or not (3, 11) <= sys.version_info[:2] <= (3, 13)
            or sysconfig.get_config_var('Py_GIL_DISABLED')):
        return False
    if sys.platform == 'win32' and (struct.calcsize('P') != 8
                                   or platform.machine().lower() not in ('amd64', 'x86_64')):
        return False
    if expected_venv is not None:
        return (sys.prefix != sys.base_prefix
                and Path(sys.prefix).resolve() == Path(expected_venv).resolve())
    return True


def venv_python(root):
    return root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def valid_venv(root):
    executable = venv_python(root)
    if not executable.is_file():
        return False
    try:
        result = subprocess.run([str(executable), '-I', '-X', 'utf8', str(Path(__file__).resolve()),
                                 '--probe', '--venv', str(root / '.venv')],
                                capture_output=True, timeout=15)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def checked(command, stage, root):
    print(stage, flush=True)
    result = subprocess.run(command, cwd=root)
    if result.returncode:
        raise RuntimeError(f'{stage}失败（退出码 {result.returncode}）。请查看上方错误，处理后重新双击启动。')


def prepare_venv(root):
    if valid_venv(root):
        print('复用项目虚拟环境 .venv。', flush=True)
        return venv_python(root)
    directory = root / '.venv'
    if directory.exists() or directory.is_symlink():
        backup = root / ('.venv.backup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        directory.rename(backup)
        print(f'原虚拟环境不可用，已备份到 {backup.name}。', flush=True)
    # A moved/broken venv may have supplied this interpreter. Use its base Python
    # to avoid nesting a newly created environment inside an old one.
    base_python = getattr(sys, '_base_executable', None) or sys.executable
    checked([base_python, '-I', '-X', 'utf8', '-m', 'venv', str(directory)], '正在创建虚拟环境……', root)
    if not valid_venv(root):
        raise RuntimeError('新虚拟环境验证失败。请检查 Python 安装和项目目录写入权限。')
    return venv_python(root)


def check_dependencies(requirements):
    """Executed in the target venv, isolated from external PYTHONPATH/settings."""
    try:
        from packaging.requirements import Requirement
    except ImportError:
        print('需要安装依赖检查工具 packaging。', flush=True)
        return 1
    try:
        missing = []
        for line in requirements.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            requirement = Requirement(line)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            try:
                installed = metadata.version(requirement.name)
            except metadata.PackageNotFoundError:
                missing.append(str(requirement))
                continue
            if not requirement.specifier.contains(installed, prereleases=False):
                missing.append(f'{requirement}（当前 {installed}）')
        if missing:
            print('需要安装或修复：' + '、'.join(missing), flush=True)
            return 1
        result = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-m', 'pip', 'check'],
                                capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode:
            print(result.stdout + result.stderr, flush=True)
            return 2
        for module in MODULES:
            importlib.import_module(module)
    except (ImportError, OSError, ValueError) as exc:
        print(f'依赖检查未通过：{exc}', flush=True)
        return 2
    return 0


def prepare_dependencies(executable, root):
    python = str(executable)
    result = subprocess.run([python, '-I', '-X', 'utf8', '-m', 'pip', '--version'], cwd=root,
                            capture_output=True)
    if result.returncode:
        checked([python, '-I', '-X', 'utf8', '-m', 'ensurepip', '--upgrade'], '正在恢复 pip……', root)
    check = [python, '-I', '-X', 'utf8', str(Path(__file__).resolve()), '--check-dependencies',
             str(root / 'requirements.txt')]
    print('正在检查项目依赖……', flush=True)
    status = subprocess.run(check, cwd=root).returncode
    if status:
        # Metadata may be intact while a DLL/package file is broken. A normal
        # pip install would say "already satisfied" without fixing that case.
        repair = ['--force-reinstall'] if status == 2 else []
        checked([python, '-I', '-X', 'utf8', '-m', 'pip', '--disable-pip-version-check', 'install',
                 '--retries', '2', '--timeout', '30', *repair, '-r', str(root / 'requirements.txt')],
                '正在安装或修复依赖（首次安装需要联网，请保持窗口开启）……', root)
        checked(check, '正在验证安装结果……', root)
    else:
        print('依赖完整，无需下载安装。', flush=True)
    # Config/source errors are separate from dependency repair; do not repeatedly
    # download packages for e.g. an invalid PORT in .env.
    checked([python, '-I', '-X', 'utf8', '-c',
             'import sys; sys.path.insert(0, sys.argv[1]); import backend.app', str(root)],
            '正在检查应用入口……', root)


def start_service(executable, root):
    print('环境已就绪，正在启动试讲；浏览器将在服务就绪后打开。', flush=True)
    command = [str(executable), '-X', 'utf8', str(root / 'run.py'), '--open-browser']
    if os.name != 'nt':
        os.chdir(root)
        os.execv(str(executable), command)
    process = subprocess.Popen(command, cwd=root)
    try:
        return process.wait()
    except KeyboardInterrupt:
        # The foreground child also receives Ctrl+C; allow its cleanup to finish.
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait()
        return 130


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--venv', type=Path)
    parser.add_argument('--check-dependencies', type=Path)
    parser.add_argument('--prepare-only', action='store_true', help='Prepare without starting the server (CI).')
    args = parser.parse_args()
    if args.probe:
        if supported_python(args.venv):
            print(sys.executable)
            return 0
        return 1
    if not supported_python():
        print('需要标准版 Python 3.11–3.13（推荐 3.13；Windows 需要 x64）。', file=sys.stderr)
        return 1
    if args.check_dependencies:
        return check_dependencies(args.check_dependencies)
    try:
        executable = prepare_venv(ROOT)
        prepare_dependencies(executable, ROOT)
        return 0 if args.prepare_only else start_service(executable, ROOT)
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'启动失败：{exc}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
