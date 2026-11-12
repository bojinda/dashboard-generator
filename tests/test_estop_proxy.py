"""Scoped E-stop proxy; actual ASGI Request/route, no production requests."""
import json,sys,tempfile,unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock,MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from starlette.requests import Request
from recovery_routes import create_router

class Proxy(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.settings={'runner_origin':'http://127.0.0.1:1'};self.tokens={}
        for n in ('ha_read','ha_operate','runner_read','runner_operate'):
            self.tokens[n]=n+'-'+'x'*64;p=Path(self.tmp.name)/n;p.write_text(self.tokens[n]);self.settings[n+'_file']=str(p)
        self.opener=Mock()
        with patch('urllib.request.build_opener',return_value=self.opener):self.route=create_router(self.settings).routes[0].endpoint
    async def call(self,action,token='ha_operate',body=b'{}',method='POST'):
        async def receive():return {'type':'http.request','body':body,'more_body':False}
        request=Request({'type':'http','method':method,'path':'/gpu-recovery/'+action,
                         'headers':[(b'authorization',('Bearer '+self.tokens[token]).encode()),(b'x-ha-user',b'admin')]},receive)
        r=await self.route(action,request);return r.status_code,json.loads(r.body)
    async def test_one_request_forwards_only_the_empty_body_and_scoped_admin_identity(self):
        response=MagicMock();response.status=200;response.headers=Message();response.headers['Content-Type']='application/json'
        response.read.return_value=b'{"timestamp":1,"outcome":"success","released_leases":1,"error":null}'
        response.__enter__.return_value=response;response.__exit__.return_value=False;self.opener.open.return_value=response
        code,value=await self.call('estop');self.assertEqual(code,200);self.assertEqual(value['outcome'],'success')
        self.opener.open.assert_called_once();request=self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(),'POST');self.assertTrue(request.full_url.endswith('/recovery-control/estop'))
        self.assertEqual(request.data,b'{}');self.assertEqual(request.get_header('X-ha-user'),'admin')
    async def test_read_credential_and_removed_workflows_cannot_mutate(self):
        self.assertEqual((await self.call('estop',token='ha_read'))[0],401)
        for action in ('estop-check','estop-verify'):self.assertEqual((await self.call(action))[0],404)
        self.opener.open.assert_not_called()
    async def test_lost_response_is_unknown_without_verify_requirement_or_retry(self):
        self.opener.open.side_effect=TimeoutError()
        code,value=await self.call('estop');self.assertEqual(code,503);self.assertEqual(value['outcome'],'unknown')
        self.assertNotIn('verify_required',value);self.opener.open.assert_called_once()
    async def test_ordinary_recover_uncertainty_keeps_existing_verify_behavior(self):
        self.opener.open.side_effect=TimeoutError()
        code,value=await self.call('recover');self.assertEqual(code,503);self.assertTrue(value['verify_required'])
