"""Read-only evidence for observed Codex service connections; never inspect payloads."""

import http.client
import ipaddress
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


SERVICES = {'chatgpt.com', 'api.openai.com'}
PROXY_TYPES = {'anytls', 'shadowsocks', 'shadowsocksr', 'vmess', 'vless', 'trojan',
               'hysteria', 'hysteria2', 'tuic', 'wireguard', 'socks5', 'http', 'ssh', 'mieru', 'snell'}
GROUP_TYPES = {'selector', 'urltest', 'fallback', 'loadbalance', 'relay'}


def _controller_socket():
    # Use existing local Unix APIs only; no credentials or controller settings are changed.
    candidates = [Path('/var/run/clash-verge-service/users') / str(os.getuid()) / 'verge-mihomo.sock',
                  Path(tempfile.gettempdir()) / 'verge-mihomo.sock']
    for path in candidates:
        try:
            info = path.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid in {0, os.getuid()}:
                return str(path)
        except OSError:
            continue
    return None


class _DeadlineSocket(socket.socket):
    def recv_into(self, buffer, *args):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Controller read budget exceeded')
        self.settimeout(remaining)
        return super().recv_into(buffer, *args)


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=1)
        self.path = path

    def connect(self):
        self.sock = _DeadlineSocket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.deadline = time.monotonic() + self.timeout
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def _api(path, endpoint):
    if endpoint not in {'/connections', '/proxies'}:
        raise ValueError('Unsupported diagnostic endpoint')
    connection = _UnixHTTP(path)
    try:
        connection.request('GET', endpoint)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('Controller unavailable')
        raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('Controller response exceeds diagnostic budget')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('Invalid controller response')
        return result
    finally:
        connection.close()


def _endpoint(address, port):
    try:
        ip = ipaddress.ip_address(address.strip('[]'))
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        port = int(port)
        if 0 < port <= 65535:
            return str(ip), port
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _sockets(pids):
    result = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', ','.join(map(str, pids)),
                             '-iTCP', '-sTCP:ESTABLISHED', '-Fpfn'],
                            capture_output=True, text=True, timeout=1)
    if result.returncode not in {0, 1} or result.stderr:
        raise ValueError('Socket inspection unavailable')
    found = set()
    pid = fd = None
    for line in result.stdout.splitlines():
        if line.startswith('p'):
            pid, fd = int(line[1:]), None
        elif line.startswith('f'):
            fd = line[1:]
        elif line.startswith('n') and pid in pids and fd and '->' in line:
            source, peer = line[1:].split('->', 1)
            source = _endpoint(*source.rsplit(':', 1))
            peer = _endpoint(*peer.rsplit(':', 1))
            if source and peer:
                found.add((pid, fd, source, peer))
    return found


def inspect_route(pids):
    evidence = {'status': 'unconfirmed', 'proxied': 0, 'direct': 0, 'unconfirmed': 0}
    if sys.platform != 'darwin' or not pids or len(pids) > 64:
        return evidence
    try:
        path = _controller_socket()
        if path is None:
            return evidence
        before = _sockets(pids)
        if not before:
            return evidence
        proxies = _api(path, '/proxies').get('proxies')
        entries = _api(path, '/connections').get('connections')
        if not isinstance(proxies, dict) or not isinstance(entries, list):
            return evidence
        after = _sockets(pids)
        stable = {(source, peer) for _, _, source, peer in before & after}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('metadata'), dict):
                return {**evidence, 'status': 'unconfirmed'}
            meta = entry['metadata']
            host = meta.get('host')
            if meta.get('network') != 'tcp' or not isinstance(host, str) or host.lower().rstrip('.') not in SERVICES:
                continue
            if str(meta.get('destinationPort')) != '443':
                continue
            source = _endpoint(meta.get('sourceIP'), meta.get('sourcePort'))
            if meta.get('type') in {'Tun', 'TUN', 'tun'}:
                peer = _endpoint(meta.get('destinationIP'), meta.get('destinationPort'))
            else:
                peer = _endpoint(meta.get('inboundIP'), meta.get('inboundPort'))
            if not source or not peer or (source, peer) not in stable:
                continue
            state = 'unconfirmed'
            chains = entry.get('chains')
            if isinstance(chains, list) and chains and all(isinstance(name, str) for name in chains):
                adapters = [proxies.get(name, {}) for name in chains]
                types = {adapter['type'].lower() if isinstance(adapter, dict) and isinstance(adapter.get('type'), str)
                         else '' for adapter in adapters}
                # Selected policy names alone are not evidence of a proxy hop.
                if types and types <= GROUP_TYPES | {'direct'} and 'direct' in types:
                    state = 'direct'
                elif types & PROXY_TYPES and types <= GROUP_TYPES | PROXY_TYPES:
                    state = 'proxied'
            if not (isinstance(entry.get('upload'), (int, float)) and entry['upload'] > 0 and
                    isinstance(entry.get('download'), (int, float)) and entry['download'] > 0):
                state = 'unconfirmed'
            evidence[state] += 1
        if evidence['proxied'] and not (evidence['direct'] or evidence['unconfirmed']):
            evidence['status'] = 'proxied'
        elif evidence['direct'] and not (evidence['proxied'] or evidence['unconfirmed']):
            evidence['status'] = 'direct'
    except (OSError, ValueError, TypeError, http.client.HTTPException, subprocess.SubprocessError):
        evidence['status'] = 'unconfirmed'
    return evidence
