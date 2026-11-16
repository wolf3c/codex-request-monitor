import copy
import json
import socketserver
import tempfile
import threading
import time
from pathlib import Path
import unittest
from unittest.mock import patch

import connection_route as route


SOCKETS = 'p11\nf7\nn127.0.0.1:50001->127.0.0.1:7890\n'
ENTRY = {
    'id': 'private-id',
    'metadata': {'network': 'tcp', 'type': 'HTTPS', 'sourceIP': '127.0.0.1',
                 'sourcePort': '50001', 'inboundIP': '127.0.0.1', 'inboundPort': '7890',
                 'destinationIP': '203.0.113.9', 'destinationPort': '443', 'host': 'chatgpt.com',
                 'processPath': '/private/path/codex', 'inboundUser': 'private-user'},
    'chains': ['private-node', 'private-group'], 'upload': 100, 'download': 200,
}
PROXIES = {'private-node': {'type': 'AnyTLS'}, 'private-group': {'type': 'Selector'}}


class RouteTest(unittest.TestCase):
    def setUp(self):
        platform = patch.object(route.sys, 'platform', 'darwin')
        platform.start(); self.addCleanup(platform.stop)
        controller = patch.object(route, '_controller_socket', return_value='/local/fixture.sock')
        controller.start(); self.addCleanup(controller.stop)

    def inspect(self, entries=None, proxies=None, before=SOCKETS, after=SOCKETS):
        result = lambda text: route.subprocess.CompletedProcess([], 0, text, '')
        with patch.object(route.subprocess, 'run', side_effect=[result(before), result(after)]) as run, \
             patch.object(route, '_api', side_effect=[{'proxies': PROXIES if proxies is None else proxies},
                                                     {'connections': [copy.deepcopy(ENTRY)] if entries is None else entries}]):
            evidence = route.inspect_route([11])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['timeout'], 1)
            self.assertNotIn('shell', call.kwargs)
        return evidence

    def test_confirmed_proxy_requires_stable_socket_matching_both_ends_and_transferred_data(self):
        result = self.inspect()
        self.assertEqual(result['status'], 'proxied')
        self.assertEqual(result['proxied'], 1)
        self.assertEqual(result['direct'], 0)
        encoded = json.dumps(result)
        for secret in ['private-', '203.0.113', '50001', 'chatgpt.com', 'AnyTLS']:
            self.assertNotIn(secret, encoded)

    def test_direct_alias_uses_actual_adapter_type_and_mixed_routes_are_unknown(self):
        self.assertEqual(self.inspect(proxies={**PROXIES, 'private-node': {'type': 'Direct'}})['status'], 'direct')
        second = copy.deepcopy(ENTRY)
        second['chains'] = ['direct-alias']
        result = self.inspect([copy.deepcopy(ENTRY), second], {**PROXIES, 'direct-alias': {'type': 'Direct'}})
        self.assertEqual(result['status'], 'unconfirmed')

    def test_unrelated_host_or_source_and_disappearing_or_reused_socket_never_confirm(self):
        for field, value in [('host', 'ab.chatgpt.com'), ('host', 'chatgpt.com.attacker.invalid'),
                             ('sourcePort', '50002'), ('inboundPort', '7891'), ('destinationPort', '80'),
                             ('network', 'udp')]:
            entry = copy.deepcopy(ENTRY); entry['metadata'][field] = value
            self.assertEqual(self.inspect([entry])['status'], 'unconfirmed', (field, value))
        self.assertEqual(self.inspect(after='')['status'], 'unconfirmed')
        self.assertEqual(self.inspect(after=SOCKETS.replace('f7', 'f8'))['status'], 'unconfirmed')
        self.assertEqual(self.inspect(after=SOCKETS.replace('p11', 'p12'))['status'], 'unconfirmed')

    def test_no_traffic_unknown_adapter_and_empty_chains_stay_unknown(self):
        for update in [{'upload': 0}, {'download': 0}, {'chains': []}, {'chains': ['unknown']}]:
            entry = {**copy.deepcopy(ENTRY), **update}
            self.assertEqual(self.inspect([entry])['status'], 'unconfirmed')
        self.assertEqual(self.inspect(proxies={'private-node': {'type': 'Selector'}})['status'], 'unconfirmed')
        self.assertEqual(self.inspect([])['status'], 'unconfirmed')

    def test_tun_and_ipv6_require_an_exact_socket_match(self):
        entry = copy.deepcopy(ENTRY)
        entry['metadata'].update(type='Tun', sourceIP='192.0.2.1')
        sockets = 'p11\nf7\nn192.0.2.1:50001->203.0.113.9:443\n'
        self.assertEqual(self.inspect([entry], before=sockets, after=sockets)['status'], 'proxied')
        self.assertEqual(self.inspect([entry])['status'], 'unconfirmed')
        entry = copy.deepcopy(ENTRY)
        entry['metadata'].update(sourceIP='::1', inboundIP='::1')
        sockets = 'p11\nf7\nn[::1]:50001->[::1]:7890\n'
        self.assertEqual(self.inspect([entry], before=sockets, after=sockets)['status'], 'proxied')

    def test_permissions_controller_errors_and_bad_response_fail_closed(self):
        with patch.object(route, '_controller_socket', return_value=None), patch.object(route.subprocess, 'run') as run:
            self.assertEqual(route.inspect_route([11])['status'], 'unconfirmed'); run.assert_not_called()
        with patch.object(route.subprocess, 'run', side_effect=PermissionError()):
            self.assertEqual(route.inspect_route([11])['status'], 'unconfirmed')
        for data in [{'connections': None}, {'connections': [None]}, {'connections': [dict(ENTRY, metadata=None)]}]:
            with patch.object(route, '_api', return_value=data), patch.object(route, '_sockets', return_value={(11, '7', ('127.0.0.1',50001), ('127.0.0.1',7890))}):
                self.assertEqual(route.inspect_route([11])['status'], 'unconfirmed')
        with patch.object(route, '_api', side_effect=route.http.client.HTTPException()), patch.object(route, '_sockets', return_value={(11, '7', ('127.0.0.1',50001), ('127.0.0.1',7890))}):
            self.assertEqual(route.inspect_route([11])['status'], 'unconfirmed')
        with patch.object(route.sys, 'platform', 'linux'), patch.object(route, '_controller_socket') as controller:
            self.assertEqual(route.inspect_route([11])['status'], 'unconfirmed'); controller.assert_not_called()

    def test_api_only_allows_bounded_local_gets_and_closes_connection(self):
        with patch.object(route, '_UnixHTTP') as factory:
            connection = factory.return_value
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{}'
            self.assertEqual(route._api('/fixture.sock', '/connections'), {})
            connection.request.assert_called_once_with('GET', '/connections')
            response.read.assert_called_once_with(4 * 1024 * 1024 + 1)
            connection.close.assert_called_once()
            for path in ['/restart', '/connections/private-id', '/proxies/node/delay']:
                with self.assertRaises(ValueError): route._api('/fixture.sock', path)
            response.read.return_value = b'x' * (4 * 1024 * 1024 + 1)
            with self.assertRaises(ValueError): route._api('/fixture.sock', '/connections')

    def test_slow_controller_body_cannot_extend_the_total_read_budget(self):
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(4096)
                self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n')
                for part in [b'{', b'}']:
                    time.sleep(.7)
                    try:
                        self.request.sendall(part)
                    except OSError:
                        break
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'api.sock')
            with socketserver.UnixStreamServer(path, Handler) as server:
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    with self.assertRaises((TimeoutError, route.socket.timeout)):
                        route._api(path, '/connections')
                finally:
                    server.shutdown(); worker.join()
