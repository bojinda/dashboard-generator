"""Real localhost HTTP delays and Media ASGI; synthetic jobs, never inference."""
import asyncio
import collections
import contextlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
import media_dispatch as dispatch
from aihub_gpu_runner.core import NativeLock,encode

TOKEN='synthetic-read-only-token-'+'x'*32


class DelayedRunner:
    """Only GET /resources and /jobs; no backend or production configuration."""
    def __init__(self,resources=1.333,jobs=.924):
        self.resources_delay=resources;self.jobs_delay=jobs
        self.jobs={};self.calls=[];self.stop=threading.Event()
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                assert self.headers.get('Authorization')=='Bearer '+TOKEN
                outer.calls.append(('GET',self.path))
                if self.path=='/resources':
                    delay=outer.resources_delay;code=200;value={'ready':True,'owners':{}}
                else:
                    delay=outer.jobs_delay;value=outer.jobs.get(self.path.removeprefix('/jobs/'))
                    code=200 if value else 404
                if outer.stop.wait(delay):return
                raw=encode(value or {'error':'not_found'})
                try:
                    self.send_response(code);self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                except (BrokenPipeError,ConnectionResetError):pass
            def do_POST(self):
                outer.calls.append(('POST',self.path));self.send_error(405)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=lambda:self.server.serve_forever(poll_interval=.01),daemon=True)
        self.url='http://127.0.0.1:'+str(self.server.server_port)
    def __enter__(self):self.thread.start();return self
    def __exit__(self,*args):
        self.stop.set();self.server.shutdown();self.thread.join(3);self.server.server_close()


def history(slot,http,operations=394,references=313,legacy_absence=False):
    """Same counts/proof classes as the observed inventory; generic identities."""
    records={};now=time.time()-120
    for number in range(operations):
        op=f'operation-{number:04}';jid=f'stage-fixture-{number:04}'
        record=dict(request_id=op,scope=slot.scope,status='error' if number<7 else 'ok',
                    kind='render-async',started=now,finished=now,error='runner_uncertain')
        if number<references:
            pending=dict(target='ollama',request_hash=f'{number:064x}',settled=number>=7)
            record['runner_jobs']={jid:pending}
            status=dict(request_id=jid,target='ollama',request_hash=pending['request_hash'],
                        state='failed',resources_released=True,supervision_active=False,
                        authority_revoked=number==6)
            if number!=5:http.jobs[jid]=status
        slot.store.write('operations/'+op+'.json',record);records[op]=record
        if number<5:
            slot._receipt(record,jid,pending,'terminal_released',status)
        if number==5:
            proof=dict(exact_request_absent_twice=True,runner_ready=True,minimum_completed_client_age_seconds=60)
            proof['owners' if legacy_absence else 'unrelated_owners']={}
            slot._receipt(record,jid,pending,'not_submitted',proof)
    slot.store.write('active.json',records['operation-0006'])
    with NativeLock(slot.lock_path).held():pass
    return {p:p.read_bytes() for p in slot.store.path('operations').glob('*.json')}


@contextlib.contextmanager
def fixture(http,operations=394,references=313,legacy_absence=False):
    with tempfile.TemporaryDirectory(prefix='dispatch-timeout-') as directory,patch.dict(os.environ,
            GPU_RUNNER_URL=http.url,GPU_RUNNER_TOKEN=TOKEN):
        root=Path(directory)
        slot=dispatch.DispatchOutputSlot(root/'state',[root/'wallpaper.png',root/'image_data.bin'])
        originals=history(slot,http,operations,references,legacy_absence)
        yield slot,originals


class DispatchTimeoutTests(unittest.TestCase):
    def test_observed_inventory_through_actual_media_http_status_fits_ha_caller(self):
        from tests import test_dispatch as existing
        import uvicorn
        app_fixture=existing.ASGITests('run');app_fixture.setUp()
        self.addCleanup(app_fixture.tearDown)
        with DelayedRunner() as http,fixture(http,legacy_absence=True) as (slot,originals):
            app_fixture.main._OUTPUT_SLOT=slot
            sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            server=uvicorn.Server(uvicorn.Config(app_fixture.app,log_level='critical',access_log=False))
            thread=threading.Thread(target=lambda:asyncio.run(server.serve(sockets=[sock])),daemon=True)
            thread.start();deadline=time.monotonic()+5
            try:
                while not server.started and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(server.started)
                begin=time.monotonic()
                import http.client as client
                conn=client.HTTPConnection('127.0.0.1',port,timeout=10)
                conn.request('GET','/render_status');response=conn.getresponse();value=json.loads(response.read());conn.close()
                elapsed=time.monotonic()-begin
                self.assertEqual(response.status,200);self.assertEqual(value['dispatch']['dispatch_status'],'ready')
                self.assertFalse(value['dispatch']['operator_required']);self.assertLess(elapsed,9.8)
                self.assertGreater(elapsed,6.5)
                self.assertEqual(slot.transport.timeout,2.5)
                self.assertTrue(all(method=='GET' for method,_ in http.calls))
                routes=[route for _,route in http.calls]
                self.assertEqual(collections.Counter(routes),{'/resources':3,'/jobs/stage-fixture-0005':2,'/jobs/stage-fixture-0006':1})
                self.assertEqual((routes[0],routes[-1]),('/resources','/resources'))
                absent=[n for n,route in enumerate(routes) if route=='/jobs/stage-fixture-0005']
                self.assertIn('/resources',routes[absent[0]+1:absent[1]])
                self.assertTrue(all(p.read_bytes()==raw for p,raw in originals.items()))
                self.assertEqual(len(originals),394)
                self.assertEqual(len(list(slot.store.path('operations').glob('*.json'))),394)
            finally:
                server.should_exit=True;thread.join(5);sock.close()

    def test_one_to_two_second_replies_make_durable_progress_then_finish(self):
        with DelayedRunner(resources=1.05,jobs=1.95) as http,fixture(http) as (slot,originals):
            begin=time.monotonic()
            with self.assertRaises(dispatch.DispatchBlocked) as caught:slot.reconcile_runner_jobs()
            self.assertEqual(caught.exception.reason,'output_reconciliation_bound_exceeded')
            self.assertLess(time.monotonic()-begin,9.8)
            self.assertEqual(len(list(slot.store.path('dispatch-reconciliations').glob('*.json'))),7)
            # Rebuild the client object, preserving exactly the output scope/paths.
            root=slot.store.root.parent
            restarted=dispatch.DispatchOutputSlot(slot.store.root,[root/'wallpaper.png',root/'image_data.bin'])
            restarted.reconcile_runner_jobs()
            self.assertEqual(restarted.health()['dispatch_status'],'ready')
            counts=collections.Counter(route for _,route in http.calls)
            self.assertEqual(counts['/jobs/stage-fixture-0006'],1)
            self.assertEqual(counts['/jobs/stage-fixture-0005'],4)  # 404 always revalidated
            self.assertTrue(all(method=='GET' for method,_ in http.calls))
            self.assertTrue(all(p.read_bytes()==raw for p,raw in originals.items()))

    def test_genuine_unresponsive_runner_remains_bounded_unavailable(self):
        with DelayedRunner(resources=4,jobs=0) as http,fixture(http) as (slot,originals):
            begin=time.monotonic()
            with self.assertRaises(dispatch.DispatchBlocked) as caught:slot.reconcile_runner_jobs()
            elapsed=time.monotonic()-begin
            self.assertEqual(caught.exception.reason,'runner_unavailable')
            self.assertGreater(elapsed,2.2);self.assertLess(elapsed,3.5)
            self.assertEqual(http.calls,[('GET','/resources')])
            self.assertEqual(len(list(slot.store.path('dispatch-reconciliations').glob('*.json'))),6)
            self.assertTrue(all(p.read_bytes()==raw for p,raw in originals.items()))

    def test_multiple_delayed_positive_references_resume_without_requerying_receipts(self):
        class Clock:
            now=0
            def __call__(self):return self.now
        clock=Clock()
        class GETs:
            jobs={};calls=[]
            def request(self,method,route):
                assert method=='GET';self.calls.append(route);clock.now+=1.5
                return 200,'application/json',encode({'ready':True,'owners':{}} if route=='/resources' else self.jobs[route[6:]])
        with tempfile.TemporaryDirectory() as directory:
            http=GETs();slot=dispatch.DispatchOutputSlot(directory,[Path(directory)/'out'],transport=http,monotonic=clock)
            for n in range(25):
                op=f'op-{n}';jid=f'job-{n}';pending=dict(target='ollama',request_hash=f'{n:064x}',settled=False)
                slot.store.write('operations/'+op+'.json',dict(request_id=op,scope=slot.scope,status='error',runner_jobs={jid:pending}))
                http.jobs[jid]=dict(request_id=jid,target='ollama',request_hash=pending['request_hash'],state='failed',resources_released=True,supervision_active=False)
            progress=[]
            for _ in range(10):
                try:slot.reconcile_runner_jobs();break
                except dispatch.DispatchBlocked as exc:
                    self.assertEqual(exc.reason,'output_reconciliation_bound_exceeded')
                    count=len(list(slot.store.path('dispatch-reconciliations').glob('*.json')))
                    self.assertGreater(count,progress[-1] if progress else 0);progress.append(count)
            self.assertEqual(slot.health()['dispatch_status'],'ready')
            self.assertEqual(len(list(slot.store.path('dispatch-reconciliations').glob('*.json'))),25)
            before=len(http.calls);slot.reconcile_runner_jobs()
            self.assertEqual(http.calls[before:],['/resources','/resources'])

    def test_chain_that_cannot_fit_caller_budget_refuses_without_skipping_404_proof(self):
        class Clock:
            now=0
            def __call__(self):return self.now
        clock=Clock()
        class Slow:
            calls=[]
            def request(self,method,route):
                assert method=='GET';self.calls.append(route);clock.now+=2
                return (200,'application/json',encode({'ready':True,'owners':{}})) if route=='/resources' else (404,'application/json',b'{}')
        with tempfile.TemporaryDirectory() as directory:
            http=Slow();slot=dispatch.DispatchOutputSlot(directory,[Path(directory)/'out'],transport=http,monotonic=clock,clock=lambda:1000)
            pending=dict(target='ollama',request_hash='a'*64,settled=False)
            slot.store.write('operations/old.json',dict(request_id='old',scope=slot.scope,status='error',finished=1,runner_jobs={'missing':pending}))
            with self.assertRaises(dispatch.DispatchBlocked) as caught:slot.reconcile_runner_jobs()
            self.assertEqual(caught.exception.reason,'output_reconciliation_bound_exceeded')
            self.assertEqual(http.calls,['/resources','/jobs/missing','/resources','/jobs/missing','/resources'])
            self.assertEqual(slot.health()['dispatch_status'],'blocked')


if __name__=='__main__':unittest.main(verbosity=2)
