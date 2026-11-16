"""On-demand macOS configuration inspection and loopback-only TCP checks."""

import ctypes
import ipaddress
import os
import re
import socket
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from connection_route import inspect_route


PROXY_KEYS = {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'}


def _dotenv_configuration():
    result = {'status': 'unconfirmed', 'variables': []}
    path = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / '.env'
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return result
        with path.open('rb') as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            return result
        text = raw.decode('utf-8')
    except FileNotFoundError:
        result['status'] = 'not_detected'
        return result
    except (OSError, UnicodeError):
        return result
    # shortcut: names are lexical candidates, not parsed config; use a dotenv parser if validating values.
    allowed = PROXY_KEYS | {key.lower() for key in PROXY_KEYS}
    names = re.findall(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', text, re.MULTILINE)
    result.update(status='present', variables=sorted(set(names) & allowed))
    return result


def _run(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, check=True, timeout=1).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _proxy_environment(raw):
    # KERN_PROCARGS2 includes argv before the environment; never treat argv as config.
    try:
        argc = struct.unpack_from('i', raw)[0]
        if not 0 < argc <= 4096:
            return None
        offset = raw.index(b'\0', 4) + 1
        while raw[offset] == 0:
            offset += 1
        for _ in range(argc):
            offset = raw.index(b'\0', offset) + 1
        selected = {}
        allowed = PROXY_KEYS | {key.lower() for key in PROXY_KEYS}
        for entry in raw[offset:].split(b'\0'):
            key, separator, value = entry.partition(b'=')
            name = key.decode('ascii', errors='ignore')
            if separator and name in allowed:
                selected[name] = value.decode('utf-8', errors='replace')
        return selected
    except (ValueError, IndexError, struct.error):
        return None


def _process_environment(pid):
    try:
        libc = ctypes.CDLL(None)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
        buffer = ctypes.create_string_buffer(262144)
        size = ctypes.c_size_t(len(buffer))
        if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            return None
        return _proxy_environment(buffer.raw[:size.value])
    except (OSError, AttributeError):
        return None


def _codex_processes(text):
    processes = {}
    for line in (text or '').splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            processes[int(fields[0])] = (int(fields[1]), fields[2])
    roots = {pid for pid, (_, executable) in processes.items()
             if re.search(r'/(?:Codex|ChatGPT)\.app/Contents/MacOS/(?:Codex|ChatGPT)$', executable)}
    selected = set(roots)
    for pid, (parent, executable) in processes.items():
        if not re.search(r'/codex(?:-[a-zA-Z0-9_-]+)?$', executable):
            continue
        visited = {pid}
        while parent in processes and parent not in visited:
            if parent in roots:
                selected.add(pid)
                break
            visited.add(parent)
            parent = processes[parent][0]
    return sorted(selected)


def _local_endpoint(value):
    try:
        parsed = urlsplit(value)
        defaults = {'http': 80, 'https': 443, 'socks5': 1080, 'socks5h': 1080, 'socks4': 1080}
        if parsed.scheme not in defaults or not parsed.hostname:
            return None
        host = parsed.hostname.lower()
        port = parsed.port if parsed.port is not None else defaults[parsed.scheme]
        if not 1 <= port <= 65535:
            return None
        if host == 'localhost' or ipaddress.ip_address(host).is_loopback:
            return host, port
    except (ValueError, TypeError):
        pass
    return None


def _probe(host, port):
    # Resolve localhost ourselves so no diagnostic can issue a DNS lookup.
    addresses = ['127.0.0.1', '::1'] if host == 'localhost' else [host]
    status = 'unreachable'
    for address in addresses:
        try:
            socket.create_connection((address, port), timeout=0.2).close()
            return 'reachable'
        except (ConnectionRefusedError, TimeoutError, socket.timeout):
            pass
        except OSError:
            status = 'unconfirmed'
    return status


def collect_network():
    result = {
        'checked_at': int(time.time()),
        'codex': {'status': 'unconfirmed', 'inspected': 0, 'unavailable': 0, 'variables': [], 'no_proxy': False},
        'dotenv': {'status': 'unconfirmed', 'variables': []},
        'local_proxies': [],
        'system': {'status': 'unconfirmed', 'manual': [], 'automatic': False},
        'tun': {'status': 'unconfirmed', 'interfaces': []},
        'route': {'status': 'unconfirmed', 'proxied': 0, 'direct': 0, 'unconfirmed': 0},
    }
    if sys.platform != 'darwin':
        return result

    result['dotenv'] = _dotenv_configuration()

    endpoints = {}

    def add_endpoint(value, source):
        endpoint = _local_endpoint(value)
        if endpoint:
            endpoints.setdefault(endpoint, set()).add(source)

    pids = _codex_processes(_run('/bin/ps', '-axo', 'pid=,ppid=,comm='))
    codex = result['codex']
    codex['unavailable'] = max(0, len(pids) - 64)
    variables = set()
    for pid in pids[:64]:
        env = _process_environment(pid)
        if env is None:
            codex['unavailable'] += 1
            continue
        codex['inspected'] += 1
        for name, value in env.items():
            if name.upper() == 'NO_PROXY':
                codex['no_proxy'] |= bool(value.strip())
            elif value.strip():
                variables.add(name)
                add_endpoint(value, 'codex')
    codex['variables'] = sorted(variables)
    if variables:
        codex['status'] = 'configured'
    elif codex['inspected'] and not codex['unavailable']:
        codex['status'] = 'not_detected'

    system = result['system']
    text = _run('/usr/sbin/scutil', '--proxy')
    if text is not None and '<dictionary>' in text:
        allowed = {'ProxyAutoConfigEnable', 'ProxyAutoDiscoveryEnable'}
        for protocol in ['HTTP', 'HTTPS', 'SOCKS']:
            allowed.update(protocol + suffix for suffix in ['Enable', 'Proxy', 'Port'])
        fields = {}
        for line in text.splitlines():
            match = re.fullmatch(r'\s*([A-Za-z]+)\s*:\s*(.*?)\s*', line)
            if match and match[1] in allowed:
                fields[match[1]] = match[2]
        for protocol in ['HTTP', 'HTTPS', 'SOCKS']:
            if fields.get(protocol + 'Enable') == '1':
                system['manual'].append(protocol)
                host = fields.get(protocol + 'Proxy', '')
                if ':' in host:
                    host = '[' + host + ']'
                scheme = 'socks5' if protocol == 'SOCKS' else 'http'
                add_endpoint(scheme + '://' + host + ':' + fields.get(protocol + 'Port', ''), 'system')
        system['automatic'] = any(fields.get(key) == '1' for key in ['ProxyAutoConfigEnable', 'ProxyAutoDiscoveryEnable'])
        system['status'] = 'configured' if system['manual'] or system['automatic'] else 'not_detected'

    interfaces = _run('/sbin/ifconfig', '-l')
    if interfaces is not None:
        result['tun']['interfaces'] = sorted({name for name in interfaces.split() if re.fullmatch(r'utun\d+', name)})
        result['tun']['status'] = 'detected' if result['tun']['interfaces'] else 'not_detected'

    for index, ((host, port), sources) in enumerate(sorted(endpoints.items())):
        address = ('[' + host + ']' if ':' in host else host) + ':' + str(port)
        result['local_proxies'].append({
            'address': address, 'status': _probe(host, port) if index < 8 else 'unconfirmed',
            'sources': sorted(sources),
        })
    result['route'] = inspect_route(pids)
    return result
