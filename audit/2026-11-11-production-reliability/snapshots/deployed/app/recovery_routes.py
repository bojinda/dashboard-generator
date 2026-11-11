"""Fixed scoped proxy on the existing authenticated Media service.

No command, backend address, PID, job submission or release endpoint is exposed.
The HA server supplies the authenticated human identity; credentials stay server-side.
"""
import hmac
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('redirect_forbidden')


def create_router(settings):
    origin=urllib.parse.urlsplit(settings['runner_origin'])
    if (origin.scheme not in ('http','https') or not origin.hostname or origin.username or origin.password or
            origin.path not in ('','/') or origin.query or origin.fragment):raise ValueError('fixed_runner_origin_required')
    # This origin is operator configuration on the same trusted Media/runner LAN.
    tokens={k:Path(settings[k+'_file']).read_text().strip() for k in ('ha_read','ha_operate','runner_read','runner_operate')}
    if len(set(tokens.values()))!=4 or any(len(v)<43 for v in tokens.values()):raise ValueError('distinct_scoped_credentials_required')
    router=APIRouter(prefix='/gpu-recovery')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    @router.api_route('/{action}',methods=['GET','POST'])
    async def route(action:str,request:Request):
        write=action in ('recover','emergency','verify')
        if action not in ('status','check','check-emergency','recover','emergency','verify') or request.method!=('POST' if write else 'GET'):
            return JSONResponse({'error':'unsupported_recovery_action'},404)
        auth=request.headers.get('authorization','')
        token=tokens['ha_operate' if write else 'ha_read']
        if not hmac.compare_digest(auth.encode(),('Bearer '+token).encode()):return JSONResponse({'error':'unauthorized'},401)
        actor=request.headers.get('x-ha-user','')
        if not re.fullmatch('[A-Za-z0-9_-]{1,64}',actor):return JSONResponse({'error':'human_identity_required'},400)
        raw=bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw)>16384:return JSONResponse({'error':'request_too_large'},413)
        if not write and raw:return JSONResponse({'error':'invalid_request'},400)
        def forward():
            req=urllib.request.Request(settings['runner_origin'].rstrip('/')+'/recovery-control/'+action,
                data=bytes(raw) if write else None,method=request.method,
                headers={'Authorization':'Bearer '+tokens['runner_operate' if write else 'runner_read'],
                         'X-HA-User':actor,'Content-Type':'application/json','Connection':'close'})
            try:response=opener.open(req,timeout=75)
            except urllib.error.HTTPError as exc:response=exc
            with response:
                data=response.read(131073)
                if len(data)>131072 or response.headers.get_content_type()!='application/json':raise ValueError()
                value=json.loads(data)
                if not isinstance(value,dict) or response.status not in (200,400,401,403,404,409,413,503):raise ValueError()
                if action == 'status' and response.status == 200:
                    # Add only CPU dispatch health; recovery controls/credentials unchanged.
                    from main import dispatch_status
                    value['media_output'] = dispatch_status()
                return JSONResponse(value,response.status)
        try:
            from starlette.concurrency import run_in_threadpool
            return await run_in_threadpool(forward)
        except Exception:
            return JSONResponse({'error':'runner_response_unavailable','verify_required':write,
                'message':'Read status, then verify the recorded operation. Do not repeat the confirmed request.'},503)
    return router


def configured_router():
    value=json.loads(Path(os.environ['AIHUB_MEDIA_RECOVERY_CONFIG']).read_bytes())
    return create_router(value)
