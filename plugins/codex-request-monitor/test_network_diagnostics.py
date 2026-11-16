import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import network_diagnostics as network


PROCESSES = '''
10 1 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT
11 10 /Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex
12 11 /usr/bin/python3
20 1 /unrelated/bin/codex
'''
SYSTEM = '''<dictionary> {
  HTTPEnable : 1
  HTTPProxy : 127.0.0.1
  HTTPPort : 7890
  ProxyAutoConfigEnable : 1
  ProxyAutoConfigURLString : https://private.invalid/pac?secret=hidden
}'''


class NetworkTest(unittest.TestCase):
    def setUp(self):
        routes = patch.object(network, 'inspect_route', return_value={
            'status': 'unconfirmed', 'proxied': 0, 'direct': 0, 'unconfirmed': 0,
        })
        self.routes = routes.start()
        self.addCleanup(routes.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        home = patch.object(network.Path, 'home', return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        env = patch.dict('os.environ', {'CODEX_HOME': ''})
        env.start()
        self.addCleanup(env.stop)

    def test_dotenv_reports_assignment_names_without_executing_or_exposing_values(self):
        path = self.home / '.codex' / '.env'
        self.assertEqual(network._dotenv_configuration()['status'], 'not_detected')
        path.parent.mkdir()
        path.write_text('export HTTPS_PROXY="http://user:secret@127.0.0.1:7890"\n'
                        'http_proxy=${OTHER}\nNO_PROXY=private.invalid\nAPI_KEY=hidden\n'
                        '# ALL_PROXY=socks5://127.0.0.1:9999\n')
        self.assertEqual(network._dotenv_configuration(), {
            'status': 'present', 'variables': ['HTTPS_PROXY', 'NO_PROXY', 'http_proxy'],
        })
        with patch.dict('os.environ', {'CODEX_HOME': str(self.home / 'custom')}):
            self.assertEqual(network._dotenv_configuration()['status'], 'not_detected')
        path.write_bytes(b'x' * 262145)
        self.assertEqual(network._dotenv_configuration()['status'], 'unconfirmed')
        path.write_bytes(b'\xff')
        self.assertEqual(network._dotenv_configuration()['status'], 'unconfirmed')

    def test_process_environment_excludes_arguments_and_unrelated_secrets(self):
        raw = struct.pack('i', 2) + b'/app/codex\0\0codex\0HTTP_PROXY=argument\0'
        raw += b'HTTPS_PROXY=http://127.0.0.1:7890\0API_KEY=secret\0NO_PROXY=localhost\0'
        self.assertEqual(network._proxy_environment(raw), {
            'HTTPS_PROXY': 'http://127.0.0.1:7890', 'NO_PROXY': 'localhost',
        })
        self.assertIsNone(network._proxy_environment(b'bad'))

    @patch.object(network, '_process_environment')
    @patch.object(network, '_run')
    @patch.object(network, '_probe', return_value='reachable')
    def test_only_target_processes_and_configured_loopback_endpoints_are_checked(self, probe, run, env):
        run.side_effect = [PROCESSES, SYSTEM, 'lo0 en0 utun3 utun5']
        env.side_effect = [
            {'HTTPS_PROXY': 'http://user:secret@127.0.0.1:7890/path?secret=hidden', 'NO_PROXY': 'private.invalid'},
            {'ALL_PROXY': 'socks5://remote.invalid:1080'},
        ]
        with patch.object(network.sys, 'platform', 'darwin'), patch.dict('os.environ', {'HTTP_PROXY': 'http://127.0.0.1:9999'}):
            result = network.collect_network()
        self.assertEqual([call.args[0] for call in env.call_args_list], [10, 11])
        self.assertEqual(result['codex']['status'], 'configured')
        self.routes.assert_called_once_with([10, 11])
        self.assertEqual(result['route']['status'], 'unconfirmed')
        self.assertEqual(result['codex']['inspected'], 2)
        self.assertEqual(result['system']['status'], 'configured')
        self.assertTrue(result['system']['automatic'])
        self.assertEqual(result['tun']['status'], 'detected')
        self.assertEqual(result['local_proxies'], [
            {'address': '127.0.0.1:7890', 'status': 'reachable', 'sources': ['codex', 'system']},
        ])
        probe.assert_called_once_with('127.0.0.1', 7890)
        encoded = json.dumps(result)
        for private in ['secret', 'private.invalid', 'user:', '/path', 'remote.invalid', '9999']:
            self.assertNotIn(private, encoded)

    @patch.object(network, '_process_environment', side_effect=[{}, None])
    @patch.object(network, '_run', side_effect=[PROCESSES, '<dictionary> {\n}', 'lo0 utun0'])
    @patch.object(network, '_probe')
    def test_missing_evidence_is_not_reported_as_direct_or_healthy(self, probe, run, env):
        with patch.object(network.sys, 'platform', 'darwin'):
            result = network.collect_network()
        self.assertEqual(result['codex']['status'], 'unconfirmed')
        self.assertEqual(result['codex']['unavailable'], 1)
        self.assertEqual(result['local_proxies'], [])
        self.assertEqual(result['system']['status'], 'not_detected')
        self.assertEqual(result['tun']['status'], 'detected')
        probe.assert_not_called()

    def test_unavailable_commands_and_unsupported_platform_stay_unconfirmed(self):
        with patch.object(network.sys, 'platform', 'darwin'), patch.object(network, '_run', return_value=None):
            result = network.collect_network()
        for key in ['codex', 'system', 'tun']:
            self.assertEqual(result[key]['status'], 'unconfirmed')
        with patch.object(network.sys, 'platform', 'linux'), patch.object(network, '_run') as run:
            result = network.collect_network()
            run.assert_not_called()
        self.assertEqual(result['local_proxies'], [])

    def test_proxy_endpoint_validation_does_not_resolve_remote_hosts(self):
        self.assertEqual(network._local_endpoint('socks5://[::1]:7890'), ('::1', 7890))
        self.assertEqual(network._local_endpoint('http://localhost:7890'), ('localhost', 7890))
        for invalid in ['http://remote.invalid:7890', 'http://127.0.0.1:0', 'http://127.0.0.1:99999',
                        'file:///private', 'http://127.0.0.1.attacker.invalid:7890']:
            self.assertIsNone(network._local_endpoint(invalid))

    @patch.object(network, '_run', side_effect=[PROCESSES, '<dictionary> {\n}', 'lo0'])
    @patch.object(network, '_probe', return_value='reachable')
    def test_probe_budget_leaves_additional_endpoints_unconfirmed(self, probe, run):
        names = ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']
        environments = [{key: 'http://127.0.0.1:' + str(7000 + i * 6 + j)
                         for j, key in enumerate(names)} for i in range(2)]
        with patch.object(network.sys, 'platform', 'darwin'), patch.object(network, '_process_environment', side_effect=environments):
            result = network.collect_network()
        self.assertEqual(probe.call_count, 8)
        self.assertEqual(sum(item['status'] == 'unconfirmed' for item in result['local_proxies']), 4)

    @patch.object(network.socket, 'create_connection')
    def test_local_probe_sends_no_payload_and_has_a_timeout(self, connect):
        self.assertEqual(network._probe('127.0.0.1', 7890), 'reachable')
        connect.assert_called_once_with(('127.0.0.1', 7890), timeout=0.2)
        connect.return_value.send.assert_not_called()
        connect.return_value.sendall.assert_not_called()
        connect.return_value.close.assert_called_once()
        connect.side_effect = PermissionError()
        self.assertEqual(network._probe('127.0.0.1', 7890), 'unconfirmed')
        connect.side_effect = ConnectionRefusedError()
        self.assertEqual(network._probe('127.0.0.1', 7890), 'unreachable')

    @patch.object(network, '_process_environment', return_value={'HTTPS_PROXY': 'http://127.0.0.1:7890'})
    @patch.object(network, '_run', side_effect=[PROCESSES, SYSTEM, 'lo0'])
    @patch.object(network, '_probe', side_effect=RuntimeError('inspection failure'))
    def test_unexpected_probe_errors_are_not_silently_treated_as_success(self, probe, run, env):
        with patch.object(network.sys, 'platform', 'darwin'):
            with self.assertRaises(RuntimeError):
                network.collect_network()
