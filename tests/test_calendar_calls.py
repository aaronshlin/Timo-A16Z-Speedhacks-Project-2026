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
                                   {'speaker':'agent','text':'To confirm, your choice is go, correct?'},
                                   {'speaker':'user','text':'yes'}]}})
        factory=lambda *args,**kw: real_client(transport=httpx.MockTransport(provider))
        with patch.object(settings,'LIVE_CALLS_ENABLED',True), patch.object(settings,'CALL_OPT_IN',True), \
             patch.object(main.httpx,'AsyncClient',side_effect=factory), \
             patch('app.context.calendar.create_block',side_effect=AssertionError('write')), \
             patch('app.phone.dial.send_sms',side_effect=AssertionError('SMS')):
            with TestClient(main.app) as client:
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
                    if session['status']=='resolved':
                        break
                    time.sleep(.02)
                self.assertEqual(session['status'],'resolved')
                self.assertEqual(session['decision'],'go')
                self.assertEqual(session['receiptStatus'],'dashboard_only')
                self.assertEqual(len([r for r in requests if r.method=='POST']),1)
                self.assertEqual(client.post('/sms/inbound',json={}).status_code,403)

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
