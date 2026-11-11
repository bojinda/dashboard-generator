import asyncio,hashlib,importlib,json,os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
from aihub_gpu_runner.client import OutputSlot,OutputBusy,CoordinationError,JobClient,PendingJob
from aihub_gpu_runner.core import encode,NativeLock
from media_dispatch import DispatchOutputSlot,DispatchBlocked


class ASGIClient:
    """Real FastAPI/Starlette routing and background lifecycle; no httpx dependency."""
    def __init__(self,app):self.app=app
    def request(self,method,url,headers=None):
        from urllib.parse import urlsplit
        u=urlsplit(url);events=[];sent=False
        async def receive():
            nonlocal sent
            if not sent:sent=True;return {'type':'http.request','body':b'','more_body':False}
            await asyncio.Event().wait()
        async def send(event):events.append(event)
        scope=dict(type='http',asgi={'version':'3.0'},http_version='1.1',method=method,
            scheme='http',path=u.path,raw_path=u.path.encode(),query_string=u.query.encode(),
            root_path='',headers=[(k.lower().encode(),v.encode()) for k,v in (headers or {}).items()],
            client=('127.0.0.1',1234),server=('offline',80))
        asyncio.run(self.app(scope,receive,send))
        status=next(e['status'] for e in events if e['type']=='http.response.start')
        raw=b''.join(e.get('body',b'') for e in events if e['type']=='http.response.body')
        class Response:
            status_code=status;text=raw.decode()
            def json(self):return json.loads(raw)
        return Response()
    def get(self,u,headers=None):return self.request('GET',u,headers)
    def post(self,u,headers=None):return self.request('POST',u,headers)


class Transport:
    def __init__(self):self.ready=True;self.owners={};self.jobs={};self.calls=[];self.override=None
    def request(self,method,route,payload=None):
        self.calls.append((method,route));assert method=='GET','reconciliation may never submit'
        if self.override:return self.override(method,route)
        if route=='/resources':return 200,'application/json',encode(dict(ready=self.ready,owners=self.owners))
        job=self.jobs.get(route.removeprefix('/jobs/'))
        return (200,'application/json',encode(job)) if job else (404,'application/json',b'{"error":"not_found"}')
    def status(self,job):
        code,_,raw=self.request('GET','/jobs/'+job)
        if code!=200:raise CoordinationError('runner_request_rejected_'+str(code))
        return json.loads(raw)


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.t=Transport()
        self.slot=DispatchOutputSlot(self.root,['/out/wallpaper.png','/out/image_data.bin'],transport=self.t)
    def tearDown(self):self.tmp.cleanup()
    def abandoned(self,age=120,status='error',jobs=True):
        self.old=dict(request_id='historical',scope=self.slot.scope,kind='render-async',status=status,started=time.time()-age,finished=time.time()-age,error='runner_uncertain')
        if jobs:self.old['runner_jobs']={'stage-historical':dict(target='ollama',request_hash='a'*64,settled=False)}
        self.slot.store.write('operations/historical.json',self.old);self.slot.store.write('active.json',self.old)
        self.original=(self.root/'operations/historical.json').read_bytes()
    def test_regression_original_slot_is_permanently_busy_on_missing_pre_submit_job(self):
        self.abandoned();old=OutputSlot(self.root,['/out/wallpaper.png','/out/image_data.bin'],job_client_factory=lambda:self.t)
        with self.assertRaises(OutputBusy):old.begin('render-async','new')
        self.assertFalse((self.root/'operations/new.json').exists())
    def test_absent_old_job_admits_only_a_different_new_operation_preserves_history(self):
        self.abandoned();op=self.slot.begin('render-async','later-schedule')
        self.assertEqual((self.root/'operations/historical.json').read_bytes(),self.original)
        receipts=list((self.root/'dispatch-reconciliations').glob('*.json'));self.assertEqual(len(receipts),1)
        self.assertEqual(json.loads(receipts[0].read_bytes())['outcome'],'not_submitted');op.finish({'ok':True})
        self.assertEqual(sum(r=='/jobs/stage-historical' for _,r in self.t.calls),2)
    def test_recent_uncertain_absence_waits_for_stability(self):
        self.abandoned(age=1)
        with self.assertRaisesRegex(DispatchBlocked,'not_yet_stable'):self.slot.begin('render-async','new')
    def test_resources_not_ready_during_maintenance_recovers_on_later_schedule(self):
        self.t.ready=False
        with self.assertRaisesRegex(DispatchBlocked,'maintenance'):self.slot.begin('render-async','first')
        self.assertFalse((self.root/'operations/first.json').exists());self.t.ready=True
        op=self.slot.begin('render-async','later');op.finish({'ok':True})
    def test_live_output_lock_still_excludes_parallel_writer(self):
        op=self.slot.begin('render-async','first')
        with self.assertRaises(OutputBusy):self.slot.begin('render-async','second')
        op.finish({'ok':True})
    def test_same_completed_id_is_cached_without_new_inference_or_readiness_requirement(self):
        op=self.slot.begin('render-async','stable');op.finish({'ok':True});self.t.ready=False
        self.assertTrue(self.slot.begin('render-async','stable').cached)
    def test_same_failed_id_never_replays(self):
        self.abandoned()
        with self.assertRaises(CoordinationError):self.slot.begin('render-async','historical')
    def test_lost_accepted_reply_with_live_job_is_never_replayed(self):
        self.abandoned();self.t.jobs['stage-historical']=dict(request_id='stage-historical',request_hash='a'*64,target='ollama',state='running',resources_released=False,supervision_active=True)
        with self.assertRaises(DispatchBlocked):self.slot.begin('render-async','later')
        self.assertFalse((self.root/'operations/later.json').exists())
    def test_exact_terminal_release_allows_later_request_without_replaying_old(self):
        self.abandoned();self.t.jobs['stage-historical']=dict(request_id='stage-historical',request_hash='a'*64,target='ollama',state='failed',resources_released=True,supervision_active=False)
        op=self.slot.begin('render-async','later');op.finish({'ok':True});self.assertEqual((self.root/'operations/historical.json').read_bytes(),self.original)
    def test_hash_mismatch_refuses_and_exposes_intervention(self):
        self.abandoned();self.t.jobs['stage-historical']=dict(request_id='stage-historical',request_hash='b'*64,target='ollama',state='success',resources_released=True,supervision_active=False)
        with self.assertRaises(DispatchBlocked):self.slot.begin('render-async','later')
        self.assertTrue(self.slot.health()['operator_required'])
    def test_absence_with_retained_owners_refuses(self):
        self.abandoned();self.t.owners={'gpu0':{'job_id':'other','phase':'uncertain'}}
        with self.assertRaises(DispatchBlocked):self.slot.begin('render-async','later')
    def test_healthy_unrelated_owner_keeps_existing_runner_queue_semantics(self):
        self.abandoned();self.t.owners={'gpu1':{'job_id':'ordinary','phase':'running'}}
        self.t.jobs['ordinary']=dict(request_id='ordinary',target='ollama',state='running',resources_released=False,supervision_active=True)
        op=self.slot.begin('render-async','later');op.finish({'ok':True})
    def test_real_retained_ollama_owner_is_named_instead_of_missing_historical_job(self):
        self.abandoned();self.t.owners={'gpu1':{'job_id':'real-blocker','phase':'reconciling'}}
        self.t.jobs['real-blocker']=dict(request_id='real-blocker',target='ollama',state='reconciling',error_code='ollama_cleanup_new_or_reused_process',resources_released=False,supervision_active=False)
        with self.assertRaises(DispatchBlocked):self.slot.begin('render-async','later')
        health=self.slot.health();self.assertEqual(health['reason'],'runner_owner_reconciling');self.assertTrue(health['operator_required']);self.assertEqual(health['blocking_job']['request_id'],'real-blocker')
    def test_second_lookup_change_refuses(self):
        self.abandoned();seen=[]
        def reply(m,r):
            if r=='/resources':return 200,'application/json',encode(dict(ready=True,owners={}))
            seen.append(r);return (404,'application/json',b'{}') if len(seen)==1 else (503,'application/json',b'{}')
        self.t.override=reply
        with self.assertRaises(DispatchBlocked):self.slot.begin('render-async','later')
    def test_storage_interruption_releases_lock_and_does_not_admit(self):
        self.abandoned();original=self.slot.store.write
        def write(n,v):
            if n.startswith('dispatch-reconciliations/'):raise OSError('synthetic disk failure')
            return original(n,v)
        with patch.object(self.slot.store,'write',write):
            with self.assertRaises(DispatchBlocked):self.slot.begin('render-async','later')
        self.assertFalse((self.root/'operations/later.json').exists())
        op=self.slot.begin('render-async','next');op.finish({'ok':True})
    def test_restart_preserves_resolution_and_new_scheduled_admission(self):
        self.abandoned();a=self.slot.begin('render-async','first');a.finish({'ok':True})
        b=DispatchOutputSlot(self.root,['/out/wallpaper.png','/out/image_data.bin'],transport=self.t).begin('render-async','next');b.finish({'ok':True})
        self.assertEqual(len(list((self.root/'dispatch-reconciliations').glob('*.json'))),1)
    def test_abandoned_background_operation_without_any_job_is_recorded_not_replayed(self):
        self.abandoned(status='running',jobs=False);self.old['finished']=None;self.slot.store.write('operations/historical.json',self.old)
        op=self.slot.begin('render-async','later');op.finish({'ok':True})
        receipt=json.loads(next((self.root/'dispatch-reconciliations').glob('*.json')).read_bytes())
        self.assertEqual(receipt['outcome'],'abandoned_before_submission')
    def test_missing_enumerated_record_refuses_without_admission(self):
        with patch.object(Path,'glob',return_value=[self.root/'never.json']):
            with self.assertRaisesRegex(DispatchBlocked,'reconciliation_storage_unavailable'):
                self.slot.begin('render-async','later')
    def test_status_poll_automatically_reconciles_without_creating_or_replaying_operation(self):
        self.abandoned()
        with NativeLock(self.slot.lock_path).held():pass
        health=self.slot.health(refresh=True)
        self.assertEqual(health['dispatch_status'],'ready');self.assertFalse(health['operator_required'])
        self.assertEqual(len(list((self.root/'operations').glob('*.json'))),1)
        self.assertEqual((self.root/'operations/historical.json').read_bytes(),self.original)
    def test_status_poll_never_reconciles_while_a_background_writer_is_owned(self):
        op=self.slot.begin('render-async','running');self.t.calls.clear()
        self.assertEqual(self.slot.health(refresh=True)['operation_status'],'running')
        self.assertEqual(self.t.calls,[]);op.finish({'ok':True})
    def test_status_poll_keeps_uncertain_running_job_blocked_and_visible(self):
        self.abandoned()
        with NativeLock(self.slot.lock_path).held():pass
        self.t.jobs['stage-historical']=dict(request_id='stage-historical',request_hash='a'*64,target='ollama',state='reconciling',resources_released=False,supervision_active=False)
        health=self.slot.health(refresh=True);self.assertTrue(health['operator_required']);self.assertEqual(health['dispatch_status'],'blocked')


class ASGITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        settings=dict(runner_origin='http://127.0.0.1:1')
        for k in ('ha_read','ha_operate','runner_read','runner_operate'):
            p=self.root/k;p.write_text(k+'-'+'0'*60);settings[k+'_file']=str(p)
        cfg=self.root/'recovery.json';cfg.write_text(json.dumps(settings))
        self.env=patch.dict(os.environ,dict(HA_URL='http://offline.invalid',HA_TOKEN='offline',CALENDAR_ENTITY='calendar.offline',OLLAMA_URL='http://offline.invalid',COMFY_URL='http://offline.invalid',GPU_RUNNER_CLIENT_STATE=str(self.root/'state'),AIHUB_MEDIA_RECOVERY_CONFIG=str(cfg),RECOVERY_ACK_TOKEN='offline-ack-token-'+'0'*40));self.env.start()
        for n in ('main','config','recovery_media','recovery_routes','gpu_runner'):sys.modules.pop(n,None)
        import main,recovery_media
        self.main=main;self.app=recovery_media.app;main.OUT_PATH=str(self.root/'wallpaper.png');self.t=Transport()
        main._OUTPUT_SLOT=DispatchOutputSlot(self.root/'state',[main.OUT_PATH,str(self.root/'image_data.bin')],transport=self.t)
        self.client=ASGIClient(self.app)
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def success(self):return dict(ok=True,fully_successful=True,wallpaper_generated=True)
    def test_recovery_wrapper_exports_same_main_app_and_native_async_route(self):
        self.assertIs(self.app,self.main.app)
        route=next(r for r in self.app.routes if getattr(r,'path',None)=='/render_async')
        self.assertIs(route.endpoint,self.main.render_async)
    def test_real_rest_without_request_header_creates_and_executes_background_once(self):
        with patch.object(self.main,'_render_body',side_effect=self.success) as body:
            r=self.client.post('/render_async');self.assertEqual(r.status_code,200);self.assertTrue(r.json()['queued'])
            status=self.client.get(r.json()['status_url']).json();self.assertEqual(status['status'],'completed');self.assertTrue(status['succeeded']);self.assertEqual(body.call_count,1)
    def test_later_normal_rest_request_gets_a_new_id(self):
        with patch.object(self.main,'_render_body',side_effect=self.success):
            a=self.client.post('/render_async').json();b=self.client.post('/render_async').json()
        self.assertNotEqual(a['request_id'],b['request_id'])
    def test_busy_is_http409_and_no_background_task_or_false_ok(self):
        op=self.main._OUTPUT_SLOT.begin('render-async','held')
        r=self.client.post('/render_async');self.assertEqual(r.status_code,409);self.assertFalse(r.json()['detail']['queued']);op.finish({'ok':True})
    def test_maintenance_is_http503_then_later_request_automatically_succeeds(self):
        self.t.ready=False;r=self.client.post('/render_async');self.assertEqual(r.status_code,503)
        self.t.ready=True
        with patch.object(self.main,'_render_body',side_effect=self.success):self.assertTrue(self.client.post('/render_async').json()['queued'])
    def test_async_failure_is_visible_by_exact_id_with_bounded_reason(self):
        with patch.object(self.main,'_render_body',side_effect=RuntimeError('private synthetic secret')):r=self.client.post('/render_async')
        status=self.client.get(r.json()['status_url']);self.assertEqual(status.json()['status'],'failed');self.assertFalse(status.json()['ok']);self.assertNotIn('private synthetic secret',status.text)
    def test_cached_success_says_cached_and_does_not_execute_again(self):
        with patch.object(self.main,'_render_body',side_effect=self.success) as body:
            self.client.post('/render_async',headers={'X-Request-ID':'repeat'})
            r=self.client.post('/render_async',headers={'X-Request-ID':'repeat'})
        self.assertTrue(r.json()['cached']);self.assertFalse(r.json()['queued']);self.assertEqual(body.call_count,1)
    def test_invalid_request_id_stays_400(self):self.assertEqual(self.client.post('/render_async',headers={'X-Request-ID':'../bad'}).status_code,400)
    def test_unknown_operation_is404_not_success(self):self.assertEqual(self.client.get('/render_status?request_id=missing').status_code,404)
    def test_acknowledge_and_recovery_routes_preserved_and_protected(self):
        self.assertEqual(self.client.post('/recovery-counter/acknowledge').status_code,401)
        self.assertEqual(self.client.get('/gpu-recovery/status').status_code,401)
        self.assertEqual(self.client.post('/gpu-recovery/recover').status_code,401)
    def test_scoped_recovery_status_adds_dispatch_without_changing_recovery_controls(self):
        import recovery_routes
        class Response:
            status=200
            class Headers:
                def get_content_type(self):return 'application/json'
            headers=Headers()
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):return encode(dict(available=True,readiness=True,operator_required=False))
        with patch('urllib.request.OpenerDirector.open',return_value=Response()):
            r=self.client.get('/gpu-recovery/status',headers={'Authorization':'Bearer '+(self.root/'ha_read').read_text(),'X-HA-User':'offline'})
        self.assertEqual(r.status_code,200);self.assertTrue(r.json()['readiness']);self.assertIn('media_output',r.json())

    def test_real_uvicorn_http_request_runs_background_without_test_client_dependency(self):
        import http.client,socket,threading,uvicorn
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(self.app,log_level='critical',access_log=False))
        thread=threading.Thread(target=lambda:asyncio.run(server.serve(sockets=[sock])),daemon=True)
        thread.start();deadline=time.monotonic()+5
        while not server.started and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(server.started)
        try:
            with patch.object(self.main,'_render_body',side_effect=self.success) as body:
                conn=http.client.HTTPConnection('127.0.0.1',port,timeout=5);conn.request('POST','/render_async',body='{}',headers={'Content-Type':'application/json'})
                response=conn.getresponse();queued=json.loads(response.read());self.assertEqual(response.status,200);conn.close()
                while time.monotonic()<deadline:
                    conn=http.client.HTTPConnection('127.0.0.1',port,timeout=5);conn.request('GET',queued['status_url']);response=conn.getresponse();status=json.loads(response.read());conn.close()
                    if status['status']=='completed':break
                    time.sleep(.01)
                self.assertEqual(status['status'],'completed');self.assertEqual(body.call_count,1)
        finally:server.should_exit=True;thread.join(5);sock.close()


if __name__=='__main__':unittest.main(verbosity=2)
