import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastmcp import Client
from app.agent import voice_mcp


class FakeEngine:
    def __init__(self, root):
        self.path = Path(root) / 'engine.json'
        self.env = {'OPERATOR_TOKEN': 'private-unit', 'DIAL_NUMBER': '+15555550123',
                    'USER_PHONE': '+15555550987', 'LIVE_CALLS_ENABLED': 'true', 'VOICE_TOOLS_ENABLED': 'true'}
        self.calls = []
        self.connections = [{'id': 'unrelated', 'name': 'Other integration', 'url': 'https://other.invalid/mcp',
                             'status': 'connected', 'authMode': 'static', 'toolCount': 1}]
        self.tool_count = 5

    def voice_context_valid(self, conversation_id):
        return conversation_id == 'active-conversation'

    async def api(self, method, endpoint, body=None, headers=None):
        self.calls.append((method, endpoint, body))
        if method == 'GET' and endpoint == '/context-mcps':
            return {'contextMcps': list(self.connections)}
        if method == 'POST' and endpoint == '/context-mcps':
            connection = {k: body[k] for k in ('name', 'url')}
            connection.update(id='owned-' + str(len(self.calls)), status='connected', authMode='static', toolCount=self.tool_count)
            self.connections.append(connection)
            return {'contextMcp': connection}
        if method == 'GET':
            return {'contextMcp': next(c for c in self.connections if endpoint.endswith('/' + c['id']))}
        if method == 'DELETE':
            self.connections = [c for c in self.connections if not endpoint.endswith('/' + c['id'])]
            return {}
        raise AssertionError((method, endpoint))

    async def voice_tasks(self, conversation_id):
        return {'conversation_id': conversation_id, 'tasks': []}

    async def voice_prepare(self, *args):
        return {'operation': 'prepare', 'args': list(args)}

    async def voice_confirm(self, *args):
        return {'operation': 'confirm', 'args': list(args)}

    async def voice_status(self, *args):
        return {'operation': 'status', 'args': list(args)}

    async def voice_disposition(self, *args):
        return {'operation': 'disposition', 'args': list(args)}


class VoiceMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = FakeEngine(self.temp.name)
        self.previous_engine = voice_mcp._engine
        voice_mcp.bind_engine(self.engine)
        self.headers = {'authorization': 'Bearer private-unit', 'x-dial-direction': 'outbound',
                        'x-dial-user-number': '+15555550987', 'x-dial-agent-number': '+15555550123'}

    def tearDown(self):
        voice_mcp.bind_engine(self.previous_engine)
        self.temp.cleanup()

    async def test_discovery_exposes_only_five_simulation_tools(self):
        async with Client(voice_mcp.mcp) as client:
            tools = await client.list_tools()
        self.assertEqual({t.name for t in tools}, voice_mcp.TOOL_NAMES)
        self.assertNotIn('act', {t.name for t in tools})

    async def test_connection_credentials_and_actual_call_headers_are_required(self):
        for changes in ({'authorization': 'Bearer wrong'}, {'x-dial-direction': 'inbound'},
                        {'x-dial-user-number': '+15555550000'}, {'x-dial-agent-number': '{agent_number}'},
                        {'x-dial-agent-number': ''}):
            with patch.object(voice_mcp, 'get_http_headers', return_value={**self.headers, **changes}):
                with self.assertRaises(ValueError):
                    await voice_mcp.timo_tasks('active-conversation')
        with patch.object(voice_mcp, 'get_http_headers', return_value={}):
            with self.assertRaises(ValueError):
                await voice_mcp.timo_tasks('active-conversation')

    async def test_tool_requires_active_context_and_live_readiness(self):
        with patch.object(voice_mcp, 'get_http_headers', return_value=self.headers):
            with self.assertRaises(ValueError):
                await voice_mcp.timo_tasks('other-conversation')
            self.engine.env['VOICE_TOOLS_ENABLED'] = 'false'
            with self.assertRaises(ValueError):
                await voice_mcp.timo_tasks('active-conversation')
            self.engine.env['VOICE_TOOLS_ENABLED'] = 'true'
            self.engine.env['LIVE_CALLS_ENABLED'] = 'false'
            with self.assertRaises(ValueError):
                await voice_mcp.timo_tasks('active-conversation')

    async def test_verbatim_approval_and_draft_forward_to_existing_engine(self):
        with patch.object(voice_mcp, 'get_http_headers', return_value=self.headers):
            result = await voice_mcp.timo_confirm_task('active-conversation', 'task-2', 'nonce',
                'Yes, send exactly that.', 'Send Sarah that exact reply?', 'Dear Sarah, Thursday works.')
            self.assertEqual(result['args'], ['active-conversation', 'task-2', 'nonce',
                'Yes, send exactly that.', 'Send Sarah that exact reply?', 'Dear Sarah, Thursday works.'])
            with self.assertRaises(ValueError):
                await voice_mcp.timo_disposition('active-conversation', 'task-2', 'go')
        self.assertEqual(self.engine.calls, [])  # Tools never invoke Dial or external action APIs.

    async def test_registration_restart_and_shutdown_only_remove_owned_connection(self):
        first = await voice_mcp.connect_context(self.engine, 'https://first.trycloudflare.com')
        self.assertEqual(first, {'connected': True, 'tool_count': 5})
        self.assertEqual(self.engine.env['VOICE_TOOLS_ENABLED'], 'false')
        first_id = next(c['id'] for c in self.engine.connections if c['id'] != 'unrelated')
        await voice_mcp.connect_context(self.engine, 'https://second.trycloudflare.com')
        self.assertNotIn(first_id, [c['id'] for c in self.engine.connections])
        metadata = voice_mcp._owned(self.engine)
        self.assertEqual(metadata['url'], 'https://second.trycloudflare.com/voice-mcp/')
        self.assertNotIn('private-unit', voice_mcp._owned_path(self.engine).read_text())
        self.assertEqual(voice_mcp._owned_path(self.engine).stat().st_mode & 0o777, 0o600)
        await voice_mcp.disconnect_context(self.engine)
        self.assertEqual([c['id'] for c in self.engine.connections], ['unrelated'])
        self.assertTrue(all(not (m == 'DELETE' and e.endswith('/unrelated')) for m, e, _ in self.engine.calls))

    async def test_incomplete_discovery_and_nonlive_mode_fail_closed(self):
        self.engine.tool_count = 4
        with self.assertRaises(ValueError):
            await voice_mcp.connect_context(self.engine, 'https://first.trycloudflare.com')
        self.assertEqual(self.engine.env['VOICE_TOOLS_ENABLED'], 'false')
        self.engine.env['LIVE_CALLS_ENABLED'] = 'false'
        before = len(self.engine.calls)
        with self.assertRaises(ValueError):
            await voice_mcp.connect_context(self.engine, 'https://first.trycloudflare.com')
        self.assertEqual(len(self.engine.calls), before)
        for origin in ('http://localhost', 'https://secret@host.invalid', 'https://host.invalid/path', 'https://host.invalid?token=private'):
            with self.assertRaises(ValueError):
                await voice_mcp.connect_context(self.engine, origin)


if __name__ == '__main__':
    unittest.main()
