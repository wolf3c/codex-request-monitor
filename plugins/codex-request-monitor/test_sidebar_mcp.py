import unittest
from http.client import HTTPException
from unittest.mock import patch

from sidebar_mcp import RESOURCE_URI, dispatch


class SidebarTest(unittest.TestCase):
    def test_sidebar_exposes_external_page_entry_but_no_restart_tool(self):
        tools = dispatch('tools/list', {})['tools']
        names = [tool['name'] for tool in tools]
        self.assertIn('open_temporary_proxy_page', names)
        self.assertNotIn('start_temporary_proxy', names)
        with patch('sidebar_mcp.open_temporary_proxy_page') as opener:
            dispatch('tools/call', {'name':'open_temporary_proxy_page','arguments':{}})
            opener.assert_called_once_with()
            with self.assertRaises(ValueError):
                dispatch('tools/call', {'name':'open_temporary_proxy_page','arguments':{'url':'https://remote.invalid'}})
            opener.assert_called_once_with()
        with patch('sidebar_mcp.open_temporary_proxy_page', side_effect=HTTPException):
            self.assertTrue(dispatch('tools/call', {'name':'open_temporary_proxy_page','arguments':{}})['isError'])

    def test_entrypoint_opens_without_reading_logs(self):
        tools = {tool['name']: tool for tool in dispatch('tools/list', {})['tools']}
        opener = tools['open_request_monitor']
        self.assertEqual(opener['title'], 'Codex Request Monitor')
        self.assertEqual(opener['_meta']['openai/ui']['entrypoints'], [{'type': 'global'}])
        self.assertEqual(opener['_meta']['ui']['resourceUri'], RESOURCE_URI)
        self.assertEqual([icon['theme'] for icon in opener['icons']], ['light', 'dark'])
        self.assertTrue(all(icon['src'].startswith('data:image/svg+xml;base64,') for icon in opener['icons']))
        for tool in tools.values():
            self.assertEqual(tool['_meta']['ui']['visibility'], ['app'])
        with patch('sidebar_mcp.collect') as collect:
            self.assertEqual(dispatch('tools/call', {'name': opener['name'], 'arguments': {}}), {'content': []})
            resource = dispatch('resources/read', {'uri': RESOURCE_URI})['contents'][0]
            collect.assert_not_called()
        self.assertIn('Codex Request Monitor', resource['text'])
        self.assertIn('Codex 请求监控', resource['text'])
        self.assertIn("connect-src 'none'", resource['text'])

    def test_monitor_data_is_only_in_private_widget_metadata(self):
        fixture = {'threads': [{'title': 'synthetic-private-title'}]}
        with patch('sidebar_mcp.collect', return_value=fixture) as collect:
            result = dispatch('tools/call', {'name': 'read_request_monitor', 'arguments': {}})
            collect.assert_called_once_with()
        self.assertEqual(result, {'content': [], '_meta': {'status': fixture}})

    def test_caller_cannot_select_a_database_or_read_arbitrary_files(self):
        with patch('sidebar_mcp.collect') as collect:
            with self.assertRaises(ValueError):
                dispatch('tools/call', {'name': 'read_request_monitor', 'arguments': {'database': 'other'}})
            with self.assertRaises(ValueError):
                dispatch('resources/read', {'uri': 'file:///other'})
            collect.assert_not_called()

    def test_network_diagnostics_are_explicit_private_and_take_no_arguments(self):
        fixture = {'codex': {'status': 'unconfirmed'}, 'local_proxies': []}
        with patch('sidebar_mcp.collect_network', return_value=fixture) as inspect:
            dispatch('resources/read', {'uri': RESOURCE_URI})
            dispatch('tools/list', {})
            with patch('sidebar_mcp.collect', return_value={'threads': []}):
                dispatch('tools/call', {'name': 'read_request_monitor', 'arguments': {}})
            inspect.assert_not_called()
            result = dispatch('tools/call', {'name': 'read_network_diagnostics', 'arguments': {}})
            self.assertEqual(result, {'content': [], '_meta': {'network': fixture}})
            inspect.assert_called_once_with()
            with self.assertRaises(ValueError):
                dispatch('tools/call', {'name': 'read_network_diagnostics', 'arguments': {'proxy': 'remote.invalid'}})
            inspect.assert_called_once_with()
