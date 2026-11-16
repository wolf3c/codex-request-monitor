"""Start an already-stopped Codex with proxy variables scoped to that child only."""

import fcntl
import os
import plistlib
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import proxy_bypass_environment

from network_diagnostics import _local_endpoint, _run


_launch_lock = threading.Lock()
_launch_process = None


def find_app():
    matches = []
    for directory in [Path('/Applications'), Path.home() / 'Applications']:
        for name in ['Codex.app', 'ChatGPT.app']:
            try:
                app = (directory / name).resolve()
                data = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
                name = data.get('CFBundleExecutable')
                if data.get('CFBundleIdentifier') != 'com.openai.codex' or not isinstance(name, str):
                    continue
                executable = (app / 'Contents/MacOS' / name).resolve()
                if (not name or Path(name).name != name or
                        executable.parent != (app / 'Contents/MacOS').resolve() or
                        not executable.is_file() or not os.access(executable, os.X_OK)):
                    continue
                if (app, executable) not in matches:
                    matches.append((app, executable))
            except (OSError, ValueError, TypeError, RuntimeError, plistlib.InvalidFileException):
                continue
    return matches[0] if len(matches) == 1 else None


def _system_proxy():
    text = _run('/usr/sbin/scutil', '--proxy')
    if text is None or not text.lstrip().startswith('<dictionary> {'):
        return None
    fields, depth = {}, 0
    for line in text.splitlines():
        if line.strip().endswith('{'):
            depth += 1
        elif line.strip() == '}':
            depth -= 1
        elif depth == 1:
            match = re.fullmatch(r'\s*(HTTPS?(?:Enable|Proxy|Port))\s*:\s*(.*?)\s*', line)
            if match:
                fields[match[1]] = match[2]
    if depth != 0:
        return None
    for protocol in ['HTTPS', 'HTTP']:
        if fields.get(protocol + 'Enable') != '1':
            continue
        host, port = fields.get(protocol + 'Proxy', ''), fields.get(protocol + 'Port', '')
        url = 'http://' + ('[' + host + ']' if ':' in host else host) + ':' + port
        return url if _local_endpoint(url) else None
    return None


def _prepare():
    plan = {'ready': False, 'reason': 'platform_unsupported', 'proxy': None}
    if sys.platform != 'darwin':
        return plan, None
    target = find_app()
    if target is None:
        plan['reason'] = 'app_unconfirmed'
        return plan, None
    app, executable = target
    text = _run('/bin/ps', '-axo', 'pid=,ppid=,comm=')
    rows = [line.strip().split(None, 2) for line in (text or '').splitlines() if line.strip()]
    if not rows or any(len(row) != 3 or not row[0].isdigit() or not row[1].isdigit() for row in rows):
        plan['reason'] = 'inspection_failed'
        return plan, None
    # A running app's quiet logs do not establish that all its chats and workers are idle.
    if any(row[2] == str(executable) or row[2].startswith(str(app) + '/') for row in rows):
        plan['reason'] = 'activity_unconfirmed'
        return plan, None
    if _launch_process is not None and _launch_process.poll() is None:
        plan['reason'] = 'launch_pending'
        return plan, None
    dotenv = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / '.env'
    try:
        dotenv.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        plan['reason'] = 'dotenv_unconfirmed'
        return plan, None
    else:
        # No exact dotenv interpreter is available; never guess whether it overrides launch variables.
        plan['reason'] = 'dotenv_unconfirmed'
        return plan, None
    exclusions = os.environ.get('no_proxy', os.environ.get('NO_PROXY', ''))
    if proxy_bypass_environment('chatgpt.com', {'no': exclusions}):
        plan['reason'] = 'no_proxy_conflict'
        return plan, None
    url = _system_proxy()
    if url is None:
        plan['reason'] = 'proxy_unconfirmed'
        return plan, None
    plan.update(ready=True, reason='app_stopped', proxy=url)
    return plan, target


def inspect_launch():
    return _prepare()[0]


def verify_proxy(url):
    endpoint = _local_endpoint(url)
    if endpoint is None or not url.startswith('http://'):
        return False
    host, port = endpoint
    addresses = ['127.0.0.1', '::1'] if host == 'localhost' else [host]
    deadline = time.monotonic() + 2
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            with socket.create_connection((address, port), timeout=remaining) as connection:
                connection.sendall(b'CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: chatgpt.com:443\r\n\r\n')
                line = b''
                while len(line) < 1024 and not line.endswith(b'\n'):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    connection.settimeout(remaining)
                    chunk = connection.recv(1)
                    if not chunk:
                        break
                    line += chunk
                return re.fullmatch(rb'HTTP/1\.[01] 200(?: [^\r\n]*)?\r?\n', line) is not None
        except OSError:
            continue
    return False


def launch(expected_proxy):
    global _launch_process
    if not _launch_lock.acquire(blocking=False):
        return {'ready': False, 'reason': 'launch_pending', 'proxy': None}
    guard = None
    try:
        plan, target = _prepare()
        if not plan['ready']:
            return plan
        # Lock the existing executable read-only so separate web servers cannot launch twice.
        try:
            guard = target[1].open('rb')
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**plan, 'ready': False, 'reason': 'launch_pending'}
        except OSError:
            return {**plan, 'ready': False, 'reason': 'launch_failed'}
        if expected_proxy != plan['proxy']:
            return {**plan, 'ready': False, 'reason': 'proxy_changed'}
        if not verify_proxy(plan['proxy']):
            return {**plan, 'ready': False, 'reason': 'proxy_failed'}
        current, current_target = _prepare()
        if not current['ready']:
            return current
        if current_target != target or current['proxy'] != plan['proxy']:
            return {**current, 'ready': False, 'reason': 'proxy_changed'}
        env = dict(os.environ)
        for name in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY']:
            env[name] = env[name.lower()] = plan['proxy']
        env['NO_PROXY'] = env['no_proxy'] = env.get('no_proxy', env.get('NO_PROXY', 'localhost,127.0.0.1,::1'))
        try:
            _launch_process = subprocess.Popen(
                [str(target[1])], env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
        except OSError:
            return {**plan, 'ready': False, 'reason': 'launch_failed'}
        return {**plan, 'ready': False, 'reason': 'launch_requested'}
    finally:
        if guard is not None:
            guard.close()
        _launch_lock.release()
