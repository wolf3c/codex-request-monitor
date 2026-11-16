import os
import json
import plistlib
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import temporary_proxy as proxy

SYSTEM = '<dictionary> {\n HTTPEnable : 1\n HTTPProxy : 127.0.0.1\n HTTPPort : 7890\n}\n'


class TemporaryProxyTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.app = self.home / 'Codex.app'
        self.exe = self.app / 'Contents/MacOS/Codex'
        self.exe.parent.mkdir(parents=True)
        self.exe.write_text('#!/bin/sh\nexit 0\n')
        self.exe.chmod(0o700)
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': 'com.openai.codex', 'CFBundleExecutable': 'Codex',
        }))
        for name, args in [('sys.platform', {'new': 'darwin'}),
                           ('Path.home', {'return_value': self.home}),
                           ('find_app', {'return_value': (self.app, self.exe)}),
                           ('_run', {'side_effect': self.run_command})]:
            obj = proxy
            parts = name.split('.')
            for part in parts[:-1]:
                obj = getattr(obj, part)
            patcher = patch.object(obj, parts[-1], **args)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {'CODEX_HOME': ''})
        env.start()
        self.addCleanup(env.stop)
        self.processes = '1 0 /sbin/launchd\n'
        self.system = SYSTEM

    def run_command(self, *args):
        if args[0] == '/bin/ps':
            return self.processes
        if args[0] == '/usr/sbin/scutil':
            return self.system
        self.fail('Unexpected command: ' + str(args[0]))

    def test_running_app_is_unconfirmed_and_never_exits_or_launches(self):
        self.processes += '10 1 ' + str(self.exe) + '\n'
        with patch.object(proxy, 'verify_proxy') as verify, patch.object(proxy.subprocess, 'Popen') as spawn:
            plan = proxy.inspect_launch()
            self.assertFalse(plan['ready'])
            self.assertEqual(plan['reason'], 'activity_unconfirmed')
            result = proxy.launch('http://127.0.0.1:7890')
            self.assertEqual(result['reason'], 'activity_unconfirmed')
            verify.assert_not_called()
            spawn.assert_not_called()

    def test_missing_process_evidence_and_orphaned_app_helper_block_launch(self):
        for processes in [None, '', 'incomplete', '11 1 ' + str(self.app / 'Contents/Resources/codex')]:
            self.processes = processes
            self.assertFalse(proxy.inspect_launch()['ready'])

    def test_stopped_app_and_one_system_http_proxy_produce_ready_plan(self):
        self.assertEqual(proxy.inspect_launch(), {
            'ready': True, 'reason': 'app_stopped', 'proxy': 'http://127.0.0.1:7890',
        })
        self.system = SYSTEM.replace('127.0.0.1', 'remote.invalid')
        self.assertEqual(proxy.inspect_launch()['reason'], 'proxy_unconfirmed')
        self.system = '<dictionary> {\n ProxyAutoConfigEnable : 1\n}'
        self.assertFalse(proxy.inspect_launch()['ready'])

    def test_existing_dotenv_is_preserved_and_blocks_unverified_overrides(self):
        dotenv = self.home / '.codex/.env'
        dotenv.parent.mkdir()
        content = b'HTTPS_PROXY=http://old.invalid:8888\nAPI_KEY=synthetic\n'
        dotenv.write_bytes(content)
        with patch.object(proxy, 'verify_proxy') as verify, patch.object(proxy.subprocess, 'Popen') as spawn:
            self.assertEqual(proxy.launch('http://127.0.0.1:7890')['reason'], 'dotenv_unconfirmed')
            self.assertEqual(dotenv.read_bytes(), content)
            verify.assert_not_called()
            spawn.assert_not_called()

    def test_proxy_change_or_failed_protocol_check_never_starts_app(self):
        with patch.object(proxy, 'verify_proxy', return_value=False), patch.object(proxy.subprocess, 'Popen') as spawn:
            self.assertEqual(proxy.launch('http://127.0.0.1:9999')['reason'], 'proxy_changed')
            self.assertEqual(proxy.launch('http://127.0.0.1:7890')['reason'], 'proxy_failed')
            spawn.assert_not_called()

    def test_rechecks_app_after_protocol_probe_and_before_start(self):
        def appears(_):
            self.processes += '12 1 ' + str(self.exe) + '\n'
            return True
        with patch.object(proxy, 'verify_proxy', side_effect=appears), patch.object(proxy.subprocess, 'Popen') as spawn:
            self.assertEqual(proxy.launch('http://127.0.0.1:7890')['reason'], 'activity_unconfirmed')
            spawn.assert_not_called()

    def test_launch_uses_only_child_environment_without_configuration_writes(self):
        before = dict(os.environ)
        with patch.object(proxy, 'verify_proxy', return_value=True), patch.object(proxy.subprocess, 'Popen') as spawn:
            result = proxy.launch('http://127.0.0.1:7890')
            self.assertEqual(result['reason'], 'launch_requested')
            args, kw = spawn.call_args
            self.assertEqual(args, ([str(self.exe)],))
            self.assertTrue(kw['start_new_session'])
            self.assertFalse(kw.get('shell', False))
            for key in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']:
                self.assertEqual(kw['env'][key], 'http://127.0.0.1:7890')
            self.assertEqual(dict(os.environ), before)
            self.assertFalse((self.home / '.codex').exists())

    def test_invalid_or_unconfirmed_app_cannot_be_launched(self):
        with patch.object(proxy, 'find_app', return_value=None):
            self.assertEqual(proxy.launch('http://127.0.0.1:7890')['reason'], 'app_unconfirmed')
        with patch.object(proxy.sys, 'platform', 'linux'):
            self.assertEqual(proxy.inspect_launch()['reason'], 'platform_unsupported')

    def test_protocol_probe_uses_only_connect_without_credentials_or_request_bodies(self):
        received = []
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                received.append(self.request.recv(4096))
                self.request.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
        with socketserver.TCPServer(('127.0.0.1', 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                self.assertTrue(proxy.verify_proxy('http://127.0.0.1:' + str(server.server_address[1])))
                self.assertEqual(received, [b'CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: chatgpt.com:443\r\n\r\n'])
            finally:
                server.shutdown(); worker.join()
        for url in ['http://remote.invalid:7890', 'socks5://127.0.0.1:7890']:
            self.assertFalse(proxy.verify_proxy(url))

    def test_rejected_proxy_response_and_spawn_error_are_not_reported_as_success(self):
        with patch.object(proxy.socket, 'create_connection') as connect:
            connect.return_value.__enter__.return_value.recv.return_value = b''
            self.assertFalse(proxy.verify_proxy('http://127.0.0.1:7890'))
            connect.return_value.__enter__.return_value.recv.side_effect = [bytes([c]) for c in b'HTTP/1.1 407 Proxy Authentication Required\r\n']
            self.assertFalse(proxy.verify_proxy('http://127.0.0.1:7890'))
        with patch.object(proxy, 'verify_proxy', return_value=True), patch.object(proxy.subprocess, 'Popen', side_effect=OSError):
            self.assertEqual(proxy.launch('http://127.0.0.1:7890')['reason'], 'launch_failed')

    def test_duplicate_launch_and_dotenv_appearing_during_validation_are_blocked(self):
        proxy._launch_lock.acquire()
        try:
            self.assertEqual(proxy.launch('http://127.0.0.1:7890')['reason'], 'launch_pending')
        finally:
            proxy._launch_lock.release()
        def add_dotenv(_):
            path = self.home / '.codex/.env'
            path.parent.mkdir()
            path.write_text('HTTP_PROXY=http://old.invalid:8888')
            return True
        with patch.object(proxy, 'verify_proxy', side_effect=add_dotenv), patch.object(proxy.subprocess, 'Popen') as spawn:
            self.assertEqual(proxy.launch('http://127.0.0.1:7890')['reason'], 'dotenv_unconfirmed')
            spawn.assert_not_called()

    def test_other_monitor_process_holding_app_lock_blocks_duplicate_launch(self):
        import fcntl
        with self.exe.open('rb') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(proxy, 'verify_proxy') as verify, patch.object(proxy.subprocess, 'Popen') as spawn:
                self.assertEqual(proxy.launch('http://127.0.0.1:7890')['reason'], 'launch_pending')
                verify.assert_not_called()
                spawn.assert_not_called()

    def test_real_isolated_child_gets_proxy_variables_without_changing_parent_or_config(self):
        output = self.home / 'child-environment.json'
        self.exe.write_text('#!/usr/bin/python3\nimport os,json\nfrom pathlib import Path\n'
                            'Path(' + repr(str(output)) + ').write_text(json.dumps({k:os.environ.get(k) '
                            'for k in ["HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"]}))\n')
        before = dict(os.environ)
        with patch.object(proxy, 'verify_proxy', return_value=True):
            result = proxy.launch('http://127.0.0.1:7890')
            self.assertEqual(result['reason'], 'launch_requested')
            self.assertEqual(proxy._launch_process.wait(timeout=5), 0)
        values = json.loads(output.read_text())
        self.assertEqual(len(values), 6)
        self.assertEqual(set(values.values()), {'http://127.0.0.1:7890'})
        self.assertEqual(dict(os.environ), before)
        self.assertFalse((self.home / '.codex').exists())

    def test_no_proxy_excluding_codex_destination_blocks_launch_without_changing_it(self):
        with patch.dict(os.environ, {'no_proxy': '.chatgpt.com'}), patch.object(proxy, 'verify_proxy', return_value=True), patch.object(proxy.subprocess, 'Popen') as spawn:
            self.assertEqual(proxy.launch('http://127.0.0.1:7890')['reason'], 'no_proxy_conflict')
            self.assertEqual(os.environ['no_proxy'], '.chatgpt.com')
            spawn.assert_not_called()
