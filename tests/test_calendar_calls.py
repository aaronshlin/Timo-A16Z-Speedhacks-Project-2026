import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import os
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient
from app.config import settings, Settings
from app import clock
from app.agent import calling
from app.agent.calling import CallService
from app.agent.calendar_trigger import CalendarGap, trigger_calendar_gap
from app.agent.poller import compute_gap, run_tick_detailed
from app.agent.engine import Engine
from app.context import calendar
from app.models import Candidate, Event, Recommendation
from app.brain import state

LA = ZoneInfo('America/Los_Angeles')
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026,10,11,13,0,tzinfo=LA)
PHONE = '+15555550100'
REC = Recommendation(pick=Candidate(kind='task', id='unit-item', title='Listen to the five-minute briefing',
                                    minutes=5, source='unit'), reason='Fits your free time',
                     speech_text='You have twenty minutes. Listen to the five-minute briefing.', sms_text='unused',
                     auto_actions=['calendar_block','todoist_in_progress'])


def event(start, end):
    return Event(id=str(start), title='Busy', start=NOW.replace(hour=start,minute=0), end=NOW.replace(hour=end,minute=0))


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = patch.multiple(settings, USE_MOCKS=False, LIVE_CALLS_ENABLED=False, NOW_OVERRIDE='',
            CALL_OPT_IN=False, DEMO_PHONE=PHONE, CALENDAR_BUFFER_MINUTES=3, GOOGLE_CALENDAR_IDS=['primary'],
            CALENDAR_ACTIVE_START_HOUR=9, CALENDAR_ACTIVE_END_HOUR=18, CALL_STATE_PATH=str(self.root/'calls.db'),
            CALL_ENGINE_STATE_PATH=str(self.root/'engine.json'), CALL_TRIGGER_TOKEN='operator',
            CALENDAR_TRIGGER_TOKEN='calendar', DIAL_API_KEY='unit-only', DIAL_WEBHOOK_SECRET='whsec_unit',
            CALL_COOLDOWN_SECONDS=0, CALL_SESSION_LIMIT=5, POLLER_ENABLED=False, RECEIPT_MODE='dashboard')
        self.patch.start()
        self.clock_patch=patch('app.clock.now', return_value=NOW); self.clock_patch.start()
        self.state_patch=patch.object(state,'_DB',self.root/'memory.db'); self.state_patch.start()
        self.old_service=calling._service; calling._service=None

    def tearDown(self):
        calling._service=self.old_service
        self.state_patch.stop(); self.clock_patch.stop(); self.patch.stop(); self.tmp.cleanup()

    def test_gaps_contain_now_buffer_overlap_empty_and_trailing(self):
        self.assertEqual(compute_gap(NOW, []).minutes, 297)
        gap=compute_gap(NOW, [event(9,12)])
        self.assertEqual(gap.start.hour,12)
        self.assertEqual(compute_gap(NOW,[event(14,15)]).minutes,57)
        self.assertIsNone(compute_gap(NOW,[event(12,14),event(13,15)]))
        self.assertIsNone(compute_gap(NOW,[event(9,18)]))
        upcoming=Event(id='soon',title='Soon',start=NOW+timedelta(minutes=12),end=NOW+timedelta(minutes=20))
        self.assertIsNone(compute_gap(NOW,[upcoming]))
        upcoming.start=NOW+timedelta(minutes=13)
        self.assertEqual(compute_gap(NOW,[upcoming]).minutes,10)
        self.assertIsNone(compute_gap(NOW.replace(hour=8),[]))

    def test_pagination_and_busy_all_day_are_read(self):
        pages=[{'timeZone':'America/New_York','items':[{'id':'all-day','start':{'date':'2026-10-11'},'end':{'date':'2026-10-12'}}],
                'nextPageToken':'page-two'},
               {'items':[{'id':'timed','summary':'Second page', 'start':{'dateTime':'2026-10-11T14:00:00-07:00'},
                          'end':{'dateTime':'2026-10-11T15:00:00-07:00'}}]}]
        requests=[]
        def response(method,url,**kwargs):
            requests.append(kwargs['params'].copy())
            return httpx.Response(200,json=pages.pop(0),request=httpx.Request(method,url))
        with patch.object(calendar,'get_access_token',return_value='unit'), patch.object(calendar,'_request',side_effect=response):
            events=calendar.get_events(NOW.date())
        self.assertEqual(requests[1]['pageToken'],'page-two')
        self.assertIsNone(compute_gap(NOW,events))
        self.assertEqual(events[0].start.hour,21)

    def test_any_read_error_or_incomplete_page_suppresses(self):
        with patch.object(calendar,'get_access_token',side_effect=RuntimeError('Missing token')):
            r=run_tick_detailed()
        self.assertFalse(r['snapshot_complete'])
        self.assertEqual(r['reason'],'calendar_read_failed')
        self.assertEqual(calling.service().sessions(),[])
        response=httpx.Response(200,json={'nextPageToken':'lost'},request=httpx.Request('GET','https://example.invalid'))
        with patch.object(calendar,'get_access_token',return_value='unit'), patch.object(calendar,'_request',return_value=response):
            with self.assertRaises(calendar.CalendarReadError):
                calendar.get_events(NOW.date())

    def test_dry_run_second_tick_and_restart_use_one_reservation(self):
        with patch.object(calendar,'get_events',return_value=[]), \
             patch('app.context.calendar.create_block',side_effect=AssertionError('calendar write')), \
             patch('app.phone.dial.send_sms',side_effect=AssertionError('SMS')), \
             patch('app.agent.actions.run_auto_actions',side_effect=AssertionError('auto action')):
            first=run_tick_detailed(); second=run_tick_detailed()
            self.assertEqual(first['status'],'calls_disabled')
            self.assertEqual(second['disposition'],'duplicate')
            self.assertEqual(first['sessionId'],second['sessionId'])
            calling._service=None
            third=run_tick_detailed()
            self.assertEqual(third['sessionId'],first['sessionId'])
            self.assertEqual(len(calling.service().sessions()),1)
        s=calling.service().sessions()[0]
        # A fresh interpreter repeats the exact opportunity; no Engine or Dial client exists.
        script='''import json,sys\nfrom app.config import Settings\nfrom app.agent.calling import CallService\nfrom app.models import Recommendation\ns=json.loads(sys.argv[2]); c=CallService(Settings(_env_file=None,USE_MOCKS=False,LIVE_CALLS_ENABLED=False,DEMO_PHONE=s['phone'],CALL_STATE_PATH=sys.argv[1]))\nprint(json.dumps(c.submit(s['phone'],Recommendation.model_validate(s['recommendation']),s['opportunityId'],'restart-process')))'''
        output=subprocess.check_output([sys.executable,'-c',script,str(calling.service().path),json.dumps(s)],cwd=ROOT,text=True)
        self.assertEqual(json.loads(output)['sessionId'],first['sessionId'])
        self.assertEqual(json.loads(output)['disposition'],'duplicate')

    def test_concurrent_reservations_are_transactional(self):
        service=calling.service()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(service.submit,PHONE,REC,'same-gap',f'event-{i}') for i in range(2)]
            results=[f.result() for f in futures]
        self.assertEqual(sorted(r['disposition'] for r in results),['accepted','duplicate'])
        self.assertEqual(len(service.sessions()),1)

    def test_stale_incomplete_future_scope_and_fake_time_are_rejected(self):
        body=CalendarGap(event_id='e',observed_at=NOW,start=NOW,end=NOW+timedelta(minutes=20),
                         calendars_checked=['primary'],snapshot_complete=True)
        for changes in ({'observed_at':NOW-timedelta(minutes=2)}, {'snapshot_complete':False},
                        {'start':NOW+timedelta(minutes=1)}, {'calendars_checked':['other']}):
            self.assertEqual(trigger_calendar_gap(body.model_copy(update=changes))[1]['disposition'],'suppressed')
        with self.assertRaises(ValueError):
            run_tick_detailed(NOW.isoformat())
        with patch.object(settings,'NOW_OVERRIDE',NOW.isoformat()):
            with self.assertRaises(ValueError):
                run_tick_detailed()
        with self.assertRaises(ValueError):
            Settings(_env_file=None,DIAL_NUMBER='cmv44iugl00mt0ftbpptzmudz')

    def test_manual_command_keeps_the_call_flag(self):
        sys.path.insert(0,str(ROOT/'calling-mvp'))
        try:
            import serve_calls
            with patch.object(serve_calls,'main') as launch, patch.object(sys,'argv',['first_call.py','--call']):
                runpy.run_path(str(ROOT/'calling-mvp/first_call.py'),run_name='__main__')
            launch.assert_called_once_with(['--live','--manual-call'])
        finally:
            sys.path.pop(0)

    def test_demo_window_restricts_real_gap_and_preserves_minimum(self):
        start, end = NOW, NOW + timedelta(minutes=15)
        with patch.object(settings, 'CALL_DEMO_WINDOW_START', start.isoformat()), \
             patch.object(settings, 'CALL_DEMO_WINDOW_END', end.isoformat()):
            gap = compute_gap(NOW, [])
            self.assertEqual((gap.start, gap.end, gap.minutes), (start, end, 12))
            self.assertIsNone(compute_gap(NOW - timedelta(seconds=1), []))
            self.assertEqual(compute_gap(NOW + timedelta(minutes=2), []).minutes, 10)
            self.assertIsNone(compute_gap(NOW + timedelta(minutes=3), []))
            self.assertIsNone(compute_gap(NOW, [event(12, 14)]))
            outside = CalendarGap(event_id='outside-demo', observed_at=NOW, start=NOW,
                end=NOW + timedelta(minutes=20), calendars_checked=['primary'], snapshot_complete=True)
            self.assertEqual(trigger_calendar_gap(outside)[1]['reason'], 'outside_demo_calling_window')
            valid = outside.model_copy(update={'end': end})
            first = trigger_calendar_gap(valid)[1]
            second = trigger_calendar_gap(valid)[1]
            self.assertEqual(first['status'], 'calls_disabled')
            self.assertEqual(second['disposition'], 'duplicate')
            self.assertEqual(first['sessionId'], second['sessionId'])
        with patch.object(settings, 'CALL_DEMO_WINDOW_START', start.isoformat()), \
             patch.object(settings, 'CALL_DEMO_WINDOW_END', (start + timedelta(minutes=8)).isoformat()):
            self.assertIsNone(compute_gap(NOW, []))

    def test_public_tunnel_startup_retries_and_fails_closed(self):
        sys.path.insert(0, str(ROOT / 'calling-mvp'))
        try:
            import serve_calls
            responses = [httpx.ConnectError('Tunnel DNS not ready'),
                         httpx.Response(503), httpx.Response(200, json={'ok': True})]
            with patch.object(serve_calls.httpx, 'get', side_effect=responses) as get, \
                 patch.object(serve_calls.time, 'sleep'):
                serve_calls.wait_for_public_health('https://unit.invalid')
                self.assertEqual(get.call_count, 3)
                self.assertTrue(all(call.args[0] == 'https://unit.invalid/health' for call in get.call_args_list))
            with patch.object(serve_calls.httpx, 'get', side_effect=httpx.ConnectError('Unreachable')), \
                 patch.object(serve_calls.time, 'monotonic', side_effect=[0, 0, 61]), \
                 patch.object(serve_calls.time, 'sleep'):
                with self.assertRaisesRegex(ValueError, 'Public callback tunnel was not reachable'):
                    serve_calls.wait_for_public_health('https://unit.invalid')
        finally:
            sys.path.pop(0)

    def test_signed_ping_accepts_null_resource_and_keeps_validation(self):
        from app import main
        real_client = httpx.AsyncClient
        def provider(request):
            raise AssertionError('Readiness pings must never place calls')
        factory = lambda *args, **kw: real_client(transport=httpx.MockTransport(provider))
        payload = {'id': 'signed-null-resource-ping', 'object': 'event',
                   'type': 'webhook.ping', 'version': 1, 'createdAt': NOW.isoformat(),
                   'relatedObject': None, 'data': {}}
        def signed(client, body, event_id=None, valid_signature=True):
            raw = json.dumps(body).encode()
            stamp = str(int(time.time()))
            secret = b'whsec_unit' if valid_signature else b'wrong-secret'
            digest = hmac.new(secret, stamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
            return client.post('/webhooks/dial', content=raw, headers={
                'X-Dial-Event-ID': event_id or body['id'],
                'X-Dial-Signature': f't={stamp},v1={digest}'})
        with patch.object(main.httpx, 'AsyncClient', side_effect=factory):
            with TestClient(main.app) as client:
                calls = calling.service()
                self.assertFalse(calls.callbacks_ready)
                self.assertEqual(signed(client, payload, valid_signature=False).status_code, 401)
                self.assertEqual(signed(client, payload, event_id='different-id').status_code, 400)
                self.assertEqual(signed(client, {**payload, 'relatedObject': []}).status_code, 400)
                self.assertFalse(calls.callbacks_ready)
                response = signed(client, payload)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(calls.callbacks_ready)
                self.assertFalse(response.json()['duplicate'])
                self.assertTrue(signed(client, payload).json()['duplicate'])
                self.assertEqual(signed(client, {**payload, 'id': 'invalid-call-resource',
                    'type': 'call.ended', 'data': {'callId': 'unit-call'}}).status_code, 400)
                self.assertEqual(calls.sessions(), [])

    def test_manual_and_calendar_routes_share_engine_and_signed_result(self):
        from app import main
        requests=[]
        real_client=httpx.AsyncClient
        def provider(request):
            requests.append(request)
            if request.method=='POST':
                # The gap reservation was committed before the sole dialing function runs.
                with sqlite3.connect(self.root/'calls.db') as db:
                    self.assertEqual(db.execute('SELECT status FROM opportunities').fetchone()[0],'reserved')
                self.assertEqual(request.headers['Idempotency-Key'],calling.service().sessions()[0]['idempotencyKey'])
                self.assertEqual(json.loads(request.content)['fromNumberId'],'cmv44iugl00mt0ftbpptzmudz')
                return httpx.Response(202,json={'call':{'id':'unit-call'}})
            return httpx.Response(200,json={'call':{'id':'unit-call','duration':12,'status':{},
                'transcriptTurns':[{'speaker':'user','text':'I choose go'},
                                   {'speaker':'agent','text':calling.service().sessions()[0]['fixture']['confirmation']},
                                   {'speaker':'user','text':'yes'}]}})
        factory=lambda *args,**kw: real_client(transport=httpx.MockTransport(provider))
        with patch.object(settings,'LIVE_CALLS_ENABLED',True), patch.object(settings,'CALL_OPT_IN',True), \
             patch.object(main.httpx,'AsyncClient',side_effect=factory), \
             patch('app.context.calendar.create_block',side_effect=AssertionError('write')), \
             patch('app.phone.dial.send_sms',side_effect=AssertionError('SMS')):
            with TestClient(main.app) as client:
                calling.service().engine.env['VOICE_TOOLS_ENABLED'] = 'true'  # Isolated fake provider; runtime readiness has dedicated tests.
                calling.service().accept({'id':'verified-test-ping','type':'webhook.ping','data':{}})
                response=client.post('/voice/trigger',headers={'Authorization':'Bearer operator'},
                                     json={'event_id':'manual-unit','available_minutes':20})
                self.assertEqual(response.status_code,202,response.text)
                self.assertEqual(len(requests),1)
                self.assertEqual(client.post('/voice/trigger',headers={'Authorization':'Bearer operator'},
                    json={'event_id':'manual-unit','available_minutes':20}).json()['disposition'],'duplicate')
                gap={'event_id':'calendar-unit','observed_at':NOW.isoformat(),'start':NOW.isoformat(),
                     'end':(NOW+timedelta(minutes=20)).isoformat(),'calendars_checked':['primary'],'snapshot_complete':True}
                self.assertEqual(client.post('/voice/calendar-gap',json=gap).status_code,401)
                self.assertEqual(client.post('/voice/calendar-gap',headers={'Authorization':'Bearer calendar'},json=gap).json()['reason'],'active_or_unknown_call')
                payload={'id':'signed-unit-event','type':'call.transcribed','data':{'callId':'unit-call'}}
                raw=json.dumps(payload).encode();stamp=str(int(time.time()))
                digest=hmac.new(b'whsec_unit',stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
                response=client.post('/webhooks/dial',content=raw,headers={'X-Dial-Event-ID':payload['id'],'X-Dial-Signature':f't={stamp},v1={digest}'})
                self.assertEqual(response.status_code,200,response.text)
                for _ in range(100):
                    session=calling.service().sessions()[0]
                    if session['status']=='finished':
                        break
                    time.sleep(.02)
                self.assertEqual(session['status'],'finished')
                self.assertEqual(session['decision'],'go')
                self.assertEqual(session['receiptStatus'],'dashboard_only')
                self.assertEqual(len([r for r in requests if r.method=='POST']),1)
                self.assertEqual(client.post('/sms/inbound',json={}).status_code,400)  # Main's signed SMS receiver retains its own validation.

    def test_fixture_missed_call_never_sends_sms(self):
        async def exercise():
            engine=Engine({'LIVE_CALLS_ENABLED':'false'},self.root/'engine.json',None)
            service=calling.service(); service.bind(engine,asyncio.get_running_loop())
            service.submit(PHONE,REC,'fixture-miss','fixture-event')
            session=service.sessions()[0]
            with patch('app.phone.dial.send_sms',side_effect=AssertionError('fixture SMS forbidden')):
                await service.miss({'id':session['sessionId']},'no-answer')
                fixture=engine.state['items'][0]
                fixture.update(status='queued',callId='fixture-no-answer')
                await engine.event({'type':'call.ended','data':{'callId':'fixture-no-answer','status':'no-answer'}})
            self.assertFalse(service.sessions()[0].get('missTextSent',False))
        asyncio.run(exercise())

    def test_unanswered_call_texts_once_and_answered_call_texts_never(self):
        async def exercise():
            engine=Engine({'LIVE_CALLS_ENABLED':'false'},self.root/'engine.json',None)
            service=calling.service(); service.bind(engine,asyncio.get_running_loop())
            self.assertIsNotNone(engine.miss_handler)

            # Every case below is a real reserved session, so the only thing that can
            # stop a text is the status gate, not a missing session lookup.
            def session_for(opportunity, call_id):
                service.submit(PHONE,REC,opportunity,'ev-'+opportunity)
                row=next(s for s in service.sessions() if s['opportunityId']==opportunity)
                row['callId']=call_id
                row.pop('fixtureId',None); row.pop('fixture',None)  # Historical main-branch SMS contract.
                row['recommendation']=REC.model_dump(mode='json')
                service._update(row)
                engine.state['items'].append({'id':row['sessionId'],'callId':call_id,'status':'queued'})
                return row

            missed=session_for('op-miss','c-miss')
            session_for('op-fail','c-fail')
            session_for('op-ok','c-ok')

            sent=[]
            with patch('app.phone.dial.send_sms',side_effect=lambda number_id,to,body:(sent.append(body) or True)):
                # Rang and nobody picked up: exactly one text.
                await engine.event({'id':'e1','type':'call.ended','data':{'callId':'c-miss','status':'no-answer'}})
                self.assertEqual(len(sent),1,sent)
                self.assertIn(REC.pick.title,sent[0])
                self.assertIn('Reply go',sent[0])
                self.assertEqual(state.get_last_pick(PHONE).pick.id,REC.pick.id)
                saved=next(s for s in service.sessions() if s['sessionId']==missed['sessionId'])
                self.assertEqual(saved['missedReason'],'no-answer')
                self.assertIs(saved['missTextSent'],True)

                # A repeated webhook for the same call adds nothing.
                await engine.event({'id':'e2','type':'call.ended','data':{'callId':'c-miss','status':'no-answer'}})
                self.assertEqual(len(sent),1,sent)

                # A placement failure may never have rung, so it must not claim a miss.
                await engine.event({'id':'e3','type':'call.ended','data':{'callId':'c-fail','status':'failed'}})
                self.assertEqual(len(sent),1,sent)

                # An answered call goes to transcription and never texts.
                await engine.event({'id':'e4','type':'call.ended','data':{'callId':'c-ok','status':'completed'}})
                self.assertEqual(len(sent),1,sent)
                self.assertEqual(next(i for i in engine.state['items'] if i.get('callId')=='c-ok')['status'],'awaiting_transcript')

                # The engine's own guard blocks a repeated webhook, so exercise the
                # service guard directly: it is what protects any other caller.
                await service.miss({'id':missed['sessionId']},'no-answer')
                self.assertEqual(len(sent),1,sent)

            # A dead SMS channel must not break the webhook worker.
            retry=session_for('op-retry','c-miss2')
            with patch('app.phone.dial.send_sms',side_effect=RuntimeError('dial down')):
                await engine.event({'id':'e5','type':'call.ended','data':{'callId':'c-miss2','status':'busy'}})
            self.assertIs(next(s for s in service.sessions() if s['sessionId']==retry['sessionId']).get('missTextSent'),None)
        asyncio.run(exercise())

    def test_calendar_trigger_places_one_call_then_dedupes_after_restart(self):
        async def exercise():
            with patch.object(settings,'LIVE_CALLS_ENABLED',True), patch.object(settings,'CALL_OPT_IN',True):
                requests=[]
                def provider(request):
                    with sqlite3.connect(self.root/'calls.db') as db:
                        self.assertEqual(db.execute('SELECT status FROM opportunities').fetchone()[0],'reserved')
                    requests.append(request)
                    return httpx.Response(202,json={'call':{'id':'calendar-call'}})
                async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                    service=calling.service()
                    engine=Engine({'DIAL_API_KEY':'unit','DIAL_FROM_NUMBER_ID':settings.DIAL_FROM_NUMBER_ID,
                                   'USER_PHONE':PHONE,'DIAL_WEBHOOK_SECRET':'whsec_unit','LIVE_CALLS_ENABLED':'true'},
                                   self.root/'engine.json',client)
                    service.bind(engine,asyncio.get_running_loop())
                    with patch.object(calendar,'get_events',return_value=[]):
                        gated=await asyncio.to_thread(run_tick_detailed)
                        self.assertEqual(gated['reason'],'waiting_for_signed_callback')
                        self.assertEqual(len(requests),0)
                        service.callbacks_ready=True
                        first=await asyncio.to_thread(run_tick_detailed)
                        self.assertEqual(first['status'],'queued')
                        second=await asyncio.to_thread(run_tick_detailed)
                        self.assertEqual(second['disposition'],'duplicate')
                        calling._service=CallService()
                        calling._service.bind(Engine(engine.env,self.root/'engine.json',client),asyncio.get_running_loop())
                        third=await asyncio.to_thread(run_tick_detailed)
                        self.assertEqual(third['disposition'],'duplicate')
                        self.assertEqual(third['sessionId'],first['sessionId'])
                        self.assertEqual(len(requests),1)
        asyncio.run(exercise())

    def test_explicit_mock_calendar_feeds_same_poller_and_call_engine(self):
        async def exercise():
            mock_path = self.root / 'calendar.json'
            mock_path.write_text(json.dumps({'date': NOW.date().isoformat(), 'snapshot_complete': True, 'events': [
                {'id': 'before', 'title': 'Demo busy before', 'start': '09:00', 'end': '13:00'},
                {'id': 'after', 'title': 'Demo busy after', 'start': '13:15', 'end': '18:00'}]}))
            with patch.multiple(settings, USE_MOCKS=True, LIVE_CALLS_ENABLED=True, CALL_OPT_IN=True,
                                MOCK_CALENDAR_PATH=str(mock_path), ALLOW_DEMO_CALENDAR_CALLS=False,
                                GOOGLE_CALENDAR_IDS=['demo-local']), \
                 patch.object(calendar, 'get_access_token', side_effect=AssertionError('Google read in mock demo')):
                requests = []
                def provider(request):
                    requests.append(request)
                    self.assertEqual(request.method, 'POST')
                    self.assertEqual(json.loads(request.content)['to'], PHONE)
                    self.assertIn('Uber', json.loads(request.content)['outboundInstruction'])
                    return httpx.Response(201, json={'call': {'id': 'mock-calendar-call'}})
                async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                    calls = calling.service()
                    engine = Engine({'DIAL_API_KEY': 'unit', 'DIAL_FROM_NUMBER_ID': settings.DIAL_FROM_NUMBER_ID,
                                     'USER_PHONE': PHONE, 'DIAL_WEBHOOK_SECRET': 'whsec_unit', 'LIVE_CALLS_ENABLED': 'true'},
                                    self.root / 'engine.json', client)
                    calls.bind(engine, asyncio.get_running_loop())
                    rejected = await asyncio.to_thread(run_tick_detailed)
                    self.assertEqual(rejected['reason'], 'mock_calendar_live_call_disabled')
                    with patch.object(settings, 'ALLOW_DEMO_CALENDAR_CALLS', True):
                        gated = await asyncio.to_thread(run_tick_detailed)
                        self.assertEqual(gated['reason'], 'waiting_for_signed_callback')
                        self.assertEqual(requests, [])
                        calls.callbacks_ready = True
                        first = await asyncio.to_thread(run_tick_detailed)
                        second = await asyncio.to_thread(run_tick_detailed)
                        self.assertEqual(first['status'], 'queued')
                        self.assertEqual(second['disposition'], 'duplicate')
                        self.assertEqual(calls.sessions()[0]['fixtureId'], 'demo-ride-home')
                        self.assertEqual(calls.sessions()[0]['calendarTrigger']['source'], 'mock_calendar')
                        self.assertEqual(len(requests), 1)
                        calling._service = CallService()
                        calling.service().bind(Engine(engine.env, self.root / 'engine.json', client), asyncio.get_running_loop())
                        third = await asyncio.to_thread(run_tick_detailed)
                        self.assertEqual(third['sessionId'], first['sessionId'])
                        self.assertEqual(len(requests), 1)
                        mock_path.write_text(json.dumps({'date': '2000-01-01', 'snapshot_complete': True, 'events': []}))
                        failed = await asyncio.to_thread(run_tick_detailed)
                        self.assertEqual(failed['reason'], 'calendar_read_failed')
                        self.assertEqual(len(requests), 1)
        asyncio.run(exercise())

    def test_uncertain_placement_survives_restart_without_redial(self):
        async def exercise():
            with patch.object(settings,'LIVE_CALLS_ENABLED',True), patch.object(settings,'CALL_OPT_IN',True):
                count=[]
                def provider(request):
                    count.append(request)
                    raise httpx.ReadTimeout('unit uncertain',request=request)
                async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                    service=calling.service()
                    engine=Engine({'DIAL_API_KEY':'unit','DIAL_FROM_NUMBER_ID':settings.DIAL_FROM_NUMBER_ID,
                                   'USER_PHONE':PHONE,'DIAL_WEBHOOK_SECRET':'whsec_unit','LIVE_CALLS_ENABLED':'true'},
                                   self.root/'engine.json',client)
                    service.bind(engine,asyncio.get_running_loop())
                    service.callbacks_ready=True
                    first=await asyncio.to_thread(service.submit,PHONE,REC,'uncertain-gap','event-one')
                    self.assertEqual(first['status'],'call_unknown')
                    restarted=CallService()
                    restarted.bind(Engine(engine.env,self.root/'engine.json',client),asyncio.get_running_loop())
                    restarted.callbacks_ready=True
                    repeat=await asyncio.to_thread(restarted.submit,PHONE,REC,'uncertain-gap','event-two')
                    self.assertEqual(repeat['disposition'],'duplicate')
                    next_gap=await asyncio.to_thread(restarted.submit,PHONE,REC,'different-gap','event-three')
                    self.assertEqual(next_gap['reason'],'active_or_unknown_call')
                    self.assertEqual(len(count),1)
        asyncio.run(exercise())


if __name__=='__main__':
    unittest.main()
