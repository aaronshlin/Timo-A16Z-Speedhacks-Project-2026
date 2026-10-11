import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastmcp import Client

from app.agent import mcp_server, voice_mcp
from app.agent.engine import atomic_json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'calling-mvp'))
import setup as setup_tools
import serve_calls as launcher


class MainContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / '.env').write_text("MCP_TOKEN='private-unit-token'\n")
        self.auth = self.root / 'auth.json'
        self.auth.write_text(json.dumps({'email': 'unit@example.invalid', 'apiKey': 'unit-only'}))
        self.connections = [{'id': 'unrelated', 'name': 'Timo', 'url': 'https://other.invalid/mcp/',
                             'status': 'connected', 'authMode': 'static', 'toolCount': 8}]
        self.count = 8
        self.requests = []

    def tearDown(self):
        self.temp.cleanup()

    def provider(self, request):
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        if request.method == 'GET' and path.endswith('/context-mcps'):
            return httpx.Response(200, json={'contextMcps': list(self.connections)})
        if request.method == 'POST':
            body = json.loads(request.content)
            connection = {key: body[key] for key in ('name', 'url')}
            connection.update(id='owned-' + str(len(self.requests)), status='connected', authMode='static', toolCount=self.count)
            self.connections.append(connection)
            return httpx.Response(201, json={'contextMcp': connection})
        if request.method == 'DELETE':
            self.connections = [item for item in self.connections if not path.endswith('/' + item['id'])]
            return httpx.Response(200, json={})
        return httpx.Response(200, json={'contextMcp': next(item for item in self.connections if path.endswith('/' + item['id']))})

    def connect(self, origin):
        return setup_tools.connect_mcp(origin, root=self.root, auth_path=self.auth,
                                       transport=httpx.MockTransport(self.provider))

    def test_main_connection_is_verified_and_only_owned_connection_is_replaced(self):
        first = self.connect('https://first.invalid')
        second = self.connect('https://second.invalid')
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual([item['name'] for item in self.connections].count('Timo'), 1)
        self.assertEqual(len(self.connections), 2)
        path = self.root / '.timo-main-context-mcp.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('private-unit-token', path.read_text())
        setup_tools.disconnect_mcp(root=self.root, auth_path=self.auth, transport=httpx.MockTransport(self.provider))
        self.assertEqual([item['id'] for item in self.connections], ['unrelated'])

    def test_incomplete_main_discovery_fails_closed_and_keeps_reconciliation_metadata(self):
        self.count = 7
        with self.assertRaisesRegex(ValueError, 'not verified'):
            self.connect('https://first.invalid')
        owned = setup_tools._mcp_owned(self.root)
        self.assertEqual(owned['status'], 'verifying')
        self.assertTrue(owned['id'])
        self.assertEqual(self.connections[0]['id'], 'unrelated')

    def test_bad_origins_do_not_mutate_context_connections(self):
        for origin in ('http://localhost', 'https://secret@host.invalid', 'https://host.invalid/path', 'https://host.invalid?private=yes'):
            with self.assertRaises(ValueError):
                self.connect(origin)
        self.assertEqual(self.requests, [])

    def test_human_phone_needs_no_second_dial_number_and_stale_persona_is_not_activated(self):
        result = launcher.number_aliases('+15555550987', '+15555550123',
                    [{'id': 'second-agent', 'number': '+15555550888'}], 'second-agent')
        self.assertEqual(result['DEMO_PHONE'], result['USER_NUMBER'])
        self.assertEqual(result['TIMO_NUMBER'], '+15555550123')
        self.assertEqual(result['DIAL_USER_NUMBER_ID'], '')
        self.assertEqual(result['USER_NUMBER_ID'], '')
        persona = launcher.number_aliases('+15555550888', '+15555550123',
                    [{'id': 'second-agent', 'number': '+15555550888'}], 'second-agent')
        self.assertEqual(persona['DIAL_USER_NUMBER_ID'], 'second-agent')
        with self.assertRaises(ValueError):
            launcher.number_aliases('+15555550123', '+15555550123')

    def test_manual_reuse_rejects_wrong_target_and_incomplete_readiness(self):
        phone = '+15555550987'
        ready = {'enrolled_phone': phone, 'live_calls_enabled': True, 'signed_callback_ready': True,
                 'voice_tools_ready': True, 'main_tools_ready': True}
        launcher.require_manual_readiness(ready, phone)
        with self.assertRaisesRegex(ValueError, 'different enrolled'):
            launcher.require_manual_readiness(ready, '+15555550888')
        for key in ('live_calls_enabled', 'signed_callback_ready', 'voice_tools_ready', 'main_tools_ready'):
            with self.assertRaises(ValueError):
                launcher.require_manual_readiness({**ready, key: False}, phone)


class RuntimeReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = SimpleNamespace(path=Path(self.temp.name) / 'engine.json',
                                      env={'OPERATOR_TOKEN': 'operator', 'VOICE_TOOLS_ENABLED': 'true'})
        self.voice = voice_mcp._owned(self.engine)
        self.voice.update(id='voice-id', status='connected', url='https://unit.invalid/voice-mcp/')
        self.main = {'id': 'main-id', 'status': 'connected', 'name': 'Timo main tools unit', 'url': 'https://unit.invalid/mcp/'}
        atomic_json(voice_mcp._owned_path(self.engine), self.voice)
        atomic_json(self.engine.path.with_name('.timo-main-context-mcp.json'), self.main)
        self.main_count = 8
        async def api(method, endpoint):
            owned, count = (self.main, self.main_count) if endpoint.endswith('/main-id') else (self.voice, 5)
            return {'contextMcp': {**owned, 'status': 'connected', 'authMode': 'static', 'toolCount': count}}
        self.engine.api = api
        self.old_engine = voice_mcp._engine
        voice_mcp.bind_engine(self.engine)
        self.request = SimpleNamespace(headers={'authorization': 'Bearer operator'},
                    app=SimpleNamespace(state=SimpleNamespace(calendar_poller=None)))

    def tearDown(self):
        voice_mcp.bind_engine(self.old_engine)
        self.temp.cleanup()

    async def test_main_discovery_matches_the_eight_current_tools(self):
        async with Client(mcp_server.mcp) as client:
            names = {tool.name for tool in await client.list_tools()}
        self.assertEqual(len(names), setup_tools.MAIN_MCP_TOOL_COUNT)
        self.assertTrue({'approve_action', 'text_details', 'save_note'} <= names)

    async def test_readiness_requires_both_connections_and_signed_callbacks(self):
        with patch('app.agent.calling.service', return_value=SimpleNamespace(callbacks_ready=True)):
            self.main_count = 7
            with self.assertRaises(HTTPException) as caught:
                await voice_mcp.runtime_ready(voice_mcp.ReadyIn(), self.request)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(self.engine.env['VOICE_TOOLS_ENABLED'], 'false')
            self.assertIsNone(self.request.app.state.calendar_poller)
            self.main_count = 8
            result = await voice_mcp.runtime_ready(voice_mcp.ReadyIn(), self.request)
            self.assertEqual(result, {'ready': True, 'voice_tools_enabled': True, 'main_tools_enabled': True, 'poller_enabled': False})
        with patch('app.agent.calling.service', return_value=SimpleNamespace(callbacks_ready=False)):
            with self.assertRaises(HTTPException):
                await voice_mcp.runtime_ready(voice_mcp.ReadyIn(), self.request)
            self.assertEqual(self.engine.env['VOICE_TOOLS_ENABLED'], 'false')

    async def test_existing_single_poller_is_reused(self):
        existing = object()
        self.request.app.state.calendar_poller = existing
        with patch('app.agent.calling.service', return_value=SimpleNamespace(callbacks_ready=True)), \
             patch.object(voice_mcp.asyncio, 'create_task', side_effect=AssertionError('second poller')):
            result = await voice_mcp.runtime_ready(voice_mcp.ReadyIn(poll_calendar=True), self.request)
        self.assertTrue(result['poller_enabled'])
        self.assertIs(self.request.app.state.calendar_poller, existing)


if __name__ == '__main__':
    unittest.main()
