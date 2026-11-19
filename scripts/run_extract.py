#!/usr/bin/env python3
"""Bound one extraction source at a time; never start interactive login."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parent
LOGIN_HINT = ('请先在浏览器登录小红书，再运行 xhs login；'
              '或在自己的终端运行 xhs login --qrcode，用小红书 App 扫码。'
              '登录完成后告诉我继续。不要发送 Cookie 或验证码。')
INSTALL_HINT = ('请先安装 xiaohongshu-cli（Python 3.10+）：'
                'uv tool install xiaohongshu-cli，或 pipx install xiaohongshu-cli；'
                '确认 xhs 在 PATH 中，再继续。')
SOURCE_TIMEOUTS = {'platform': 120, 'diandian': 180}


def bounded(argv, timeout, capture=False):
    """On macOS/Linux, cancel the entire child process group on timeout."""
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None,
                          text=True, errors='replace',
                          start_new_session=os.name == 'posix') as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                if os.name == 'posix':
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
            proc.communicate()
            raise
        return proc.returncode, out, err


def cli_payload(stdout):
    try:
        value = json.loads(stdout or '')
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def cli_guidance(payload, fallback):
    error = payload.get('error')
    code = error.get('code') if isinstance(error, dict) else None
    if code == 'not_authenticated':
        return 'AUTH_REQUIRED', LOGIN_HINT
    if code == 'verification_required':
        return 'VERIFICATION_REQUIRED', '请在浏览器打开小红书并完成平台验证，然后告诉我继续。'
    if code == 'ip_blocked':
        return 'ACCESS_BLOCKED', '小红书限制了当前访问，先停止重试；稍后再试。'
    if code == 'signature_error':
        return 'CLI_SIGNATURE_ERROR', '小红书 CLI 签名失败，请检查 CLI 版本和兼容性；这不代表未登录。'
    return fallback


def fail(code, message, exit_code=1):
    print(f'[{code}] {message}', file=sys.stderr, flush=True)
    raise SystemExit(exit_code)


def point_python(xhs):
    """Resolve the installed CLI interpreter, preserving its virtualenv path."""
    entry = Path(xhs).resolve()
    candidates = []
    try:
        with entry.open('rb') as stream:
            line = stream.readline(1024).decode('utf-8', errors='replace')
        if line.startswith('#!'):
            tokens = shlex.split(line[2:])
            if tokens and Path(tokens[0]).is_absolute() and Path(tokens[0]).name.startswith('python'):
                candidates.append(Path(tokens[0]))
    except (OSError, ValueError):
        pass
    candidates.append(entry.parent / 'python')
    for python in dict.fromkeys(candidates):
        if not python.is_file() or not os.access(python, os.X_OK):
            continue
        try:
            rc, _, _ = bounded([str(python), '-B', '-c',
                                'import sys; assert sys.version_info >= (3, 10); from xhshow import Xhshow'],
                               6, capture=True)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if rc == 0:
            return str(python)
    return None


def check_login(xhs):
    try:
        rc, out, _ = bounded([xhs, 'status', '--json'], 20, capture=True)
    except subprocess.TimeoutExpired:
        fail('STATUS_TIMEOUT', '登录状态检查超过 20 秒。请检查网络后再试；目前不能判断是否未登录。', 124)
    except OSError:
        fail('CLI_UNAVAILABLE', INSTALL_HINT)
    payload = cli_payload(out)
    data = payload.get('data')
    if rc == 0 and payload.get('ok') is True and isinstance(data, dict):
        user = data.get('user')
        if data.get('authenticated') is False or (isinstance(user, dict) and user.get('guest') is True):
            fail('AUTH_REQUIRED', LOGIN_HINT)
        if data.get('authenticated') is True and isinstance(user, dict) and user.get('guest') is False:
            return
    fail(*cli_guidance(payload, ('STATUS_FAILED', '无法确认小红书登录状态。请检查网络或 xhs 版本后再试；未自动启动登录。')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', choices=SOURCE_TIMEOUTS)
    parser.add_argument('note')
    parser.add_argument('output', help='Platform workdir or Diandian output root')
    parser.add_argument('--meta', help='Existing metadata for the same note (Diandian only)')
    parser.add_argument('--archive-folder', help='Selected existing archive folder (Diandian only)')
    args = parser.parse_args()
    if args.source == 'platform' and args.archive_folder is not None:
        parser.error('平台字幕的 --archive-folder 应传给 collect.py。')
    xhs = shutil.which('xhs')
    if not xhs:
        fail('CLI_MISSING', INSTALL_HINT)
    python = None
    if args.source == 'diandian':
        if not shutil.which('curl'):
            fail('POINT_CURL_MISSING', '点点需要 curl。请先安装或修复 PATH；平台字幕不受影响。')
        python = point_python(xhs)
        if not python:
            fail('POINT_PYTHON_MISSING', '找不到 xhs 安装环境里可加载 xhshow 的 Python 3.10+。'
                 '请检查当前 xhs 安装环境；平台字幕仍可单独获取。')
    check_login(xhs)
    if args.source == 'platform':
        argv = [sys.executable, '-B', str(SCRIPTS / 'fetch.py'), args.note, args.output]
    else:
        argv = [python, '-B', str(SCRIPTS / 'diandian_transcript.py'), args.note, '--outdir', args.output]
        if args.meta:
            argv += ['--meta', args.meta]
        if args.archive_folder is not None:
            argv += ['--archive-folder', args.archive_folder]
    limit = SOURCE_TIMEOUTS[args.source]
    label = '平台字幕' if args.source == 'platform' else '点点'
    print(f'▸ 开始获取{label}（本阶段最长等待 {limit} 秒）', flush=True)
    try:
        rc, _, _ = bounded(argv, limit)
    except subprocess.TimeoutExpired:
        fail('EXTRACTION_TIMEOUT', f'{label}超过 {limit} 秒，已停止本阶段；可保留另一来源的已有结果。', 124)
    except OSError:
        fail('RUNTIME_UNAVAILABLE', f'无法启动{label}脚本，请检查 Python 运行环境。')
    if rc:
        raise SystemExit(rc if rc > 0 else 1)


if __name__ == '__main__':
    main()
