"""Expose the existing read-only dashboard as a local MCP App."""

import base64
import json
import sqlite3
import sys

from monitor import ROOT, collect


TITLE = 'Codex 请求监控'
VERSION = '0.1.0-beta'
RESOURCE_URI = 'ui://codex-request-monitor/dashboard'
RESOURCE = {'uri': RESOURCE_URI, 'name': TITLE, 'mimeType': 'text/html;profile=mcp-app'}
CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; form-action 'none'; connect-src 'none'; base-uri 'none'"
ICON = (ROOT / 'assets' / 'monitor.svg').read_text(encoding='utf-8')
TOOLS = [
    {
        'name': 'open_request_monitor',
        'title': TITLE,
        'icons': [
            {
                'src': 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode(),
                'mimeType': 'image/svg+xml',
                'sizes': ['any'],
                'theme': theme,
            }
            for theme, svg in [('light', ICON), ('dark', ICON.replace('#365a4d', '#a1d4be'))]
        ],
        '_meta': {
            'ui': {'visibility': ['app'], 'resourceUri': RESOURCE_URI},
            'openai/ui': {'entrypoints': [{'type': 'global'}]},
        },
    },
    {'name': 'read_request_monitor', '_meta': {'ui': {'visibility': ['app']}}},
]
for tool in TOOLS:
    tool['inputSchema'] = {'type': 'object', 'properties': {}, 'additionalProperties': False}
    tool['annotations'] = {'readOnlyHint': True, 'openWorldHint': False}


def dashboard_resource():
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    bridge = (ROOT / 'sidebar_ui.js').read_text(encoding='utf-8')
    html = html.replace('<title>', f'<meta http-equiv="Content-Security-Policy" content="{CSP}"><title>', 1)
    html = html.replace('<script>', f'<script>{bridge}</script><script>', 1)
    return {
        **RESOURCE,
        'text': html,
        '_meta': {
            'ui': {'csp': {'connectDomains': [], 'resourceDomains': [], 'frameDomains': []}},
            'openai/ui': {'preferredDisplayMode': 'fullscreen', 'availableDisplayModes': ['fullscreen']},
        },
    }


def dispatch(method, params):
    if method == 'initialize':
        return {
            'protocolVersion': '2025-11-25',
            'serverInfo': {'name': 'codex-request-monitor', 'version': VERSION},
            'capabilities': {'tools': {}, 'resources': {}},
        }
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': TOOLS}
    if method == 'resources/list':
        return {'resources': [RESOURCE]}
    if method == 'resources/templates/list':
        return {'resourceTemplates': []}
    if method == 'resources/read':
        if params.get('uri') != RESOURCE_URI:
            raise ValueError('Unknown resource')
        return {'contents': [dashboard_resource()]}
    if method == 'tools/call':
        if params.get('arguments', {}) != {}:
            raise ValueError('This tool takes no arguments')
        if params.get('name') == 'open_request_monitor':
            return {'content': []}
        if params.get('name') == 'read_request_monitor':
            try:
                return {'content': [], '_meta': {'status': collect()}}
            except (OSError, sqlite3.Error):
                return {'content': [], 'isError': True}
        raise ValueError('Unknown tool')
    raise NotImplementedError


if __name__ == '__main__':
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or 'id' not in message:
            continue
        response = {'jsonrpc': '2.0', 'id': message['id']}
        try:
            params = message.get('params', {})
            if not isinstance(params, dict):
                raise ValueError('Invalid parameters')
            response['result'] = dispatch(message.get('method'), params)
        except ValueError:
            response['error'] = {'code': -32602, 'message': 'Invalid parameters'}
        except NotImplementedError:
            response['error'] = {'code': -32601, 'message': 'Method not found'}
        except OSError:
            response['error'] = {'code': -32603, 'message': 'Local resource unavailable'}
        print(json.dumps(response), flush=True)
