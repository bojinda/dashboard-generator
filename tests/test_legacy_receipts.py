"""Read-time compatibility only; generic temporary receipts, GET-only replies."""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
from aihub_gpu_runner.client import CoordinationError
from aihub_gpu_runner.core import encode
from media_dispatch import DispatchBlocked
from tests import test_dispatch_progress as existing


class LegacyReceiptTests(unittest.TestCase):
    def setUp(self):
        self.f=existing.DispatchProgress('runTest');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.job,self.operation=self.f.add(1)
        record=json.loads(self.operation.read_bytes());record['kind']='render-async'
        self.operation.write_bytes(encode(record))
        self.terminal=self.f.http.jobs.pop(self.job)
        self.f.slot.reconcile_runner_jobs()
        self.receipt=self.f.receipts()[0]
        self.base=json.loads(self.receipt.read_bytes())
        self.operation_bytes=self.operation.read_bytes()

    def write(self,proof=...,**fields):
        value=copy.deepcopy(self.base)
        if proof is not ...:value['proof']=proof
        value.update(fields);self.receipt.write_bytes(encode(value))
        return self.receipt.read_bytes()

    def legacy(self):
        proof=copy.deepcopy(self.base['proof']);proof['owners']=proof.pop('unrelated_owners')
        return proof

    def refused(self,reason='runner_job_identity_mismatch'):
        raw=self.receipt.read_bytes();self.f.http.calls.clear()
        with self.assertRaises(DispatchBlocked) as caught:self.f.restart().reconcile_runner_jobs()
        self.assertEqual(caught.exception.reason,reason)
        self.assertEqual(self.receipt.read_bytes(),raw)
        self.assertEqual(self.operation.read_bytes(),self.operation_bytes)

    def test_both_formats_and_equal_empty_aliases_revalidate_without_rewriting(self):
        both=self.legacy();both['unrelated_owners']={}
        for proof in (self.base['proof'],self.legacy(),both):
            with self.subTest(proof=proof):
                original=self.write(proof);self.f.http.calls.clear()
                self.f.restart().reconcile_runner_jobs();self.f.restart().reconcile_runner_jobs()
                self.assertEqual(self.f.http.calls,['/resources','/jobs/'+self.job,'/resources','/jobs/'+self.job,'/resources']*2)
                self.assertEqual(self.receipt.read_bytes(),original)
                self.assertEqual(self.operation.read_bytes(),self.operation_bytes)

    def test_conflicts_and_invalid_current_key_never_fall_back_to_legacy(self):
        for current in (None,[],False,'',{'gpu0':{'job_id':'other'}}):
            with self.subTest(current=current):
                proof=self.legacy();proof['unrelated_owners']=current;self.write(proof)
                self.refused();self.assertEqual(self.f.http.calls,['/resources'])

    def test_legacy_is_empty_dict_only_and_missing_or_malformed_proofs_refuse(self):
        for legacy in (None,[],False,'',1,{'gpu0':{'job_id':'other'}}):
            with self.subTest(legacy=legacy):
                proof=self.legacy();proof['owners']=legacy;self.write(proof);self.refused()
        for proof in (None,[],False,''):
            with self.subTest(proof_type=type(proof).__name__):
                self.write(**{'proof':proof});self.refused()
        proof=self.legacy();proof.pop('owners');self.write(proof);self.refused()

    def test_required_predicates_and_identity_hashes_are_still_required(self):
        for key in ('exact_request_absent_twice','runner_ready','minimum_completed_client_age_seconds'):
            proof=self.legacy();proof.pop(key);self.write(proof);self.refused()
        for key,value in (('exact_request_absent_twice',False),('runner_ready',False),('minimum_completed_client_age_seconds',0)):
            proof=self.legacy();proof[key]=value;self.write(proof);self.refused()
        for key,value in (('schema',2),('scope','other'),('operation_id','different'),
                ('operation_sha256','0'*64),('runner_request_id','different'),
                ('runner_binding',dict(target='wallpaper',request_hash='0'*64,settled=False)),
                ('outcome','terminal_released'),('replayed',True),('reconciled_at',None)):
            with self.subTest(field=key):self.write(self.legacy(),**{key:value});self.refused()

    def test_current_nonempty_unrelated_owners_format_is_preserved(self):
        proof=copy.deepcopy(self.base['proof']);proof['unrelated_owners']={'gpu0':{'job_id':'other'}}
        original=self.write(proof);self.f.restart().reconcile_runner_jobs()
        self.assertEqual(self.receipt.read_bytes(),original)
        proof['unrelated_owners']={'gpu0':{'job_id':self.job}};self.write(proof);self.refused()

    def test_current_active_ownership_and_later_running_or_mismatched_job_block(self):
        self.write(self.legacy())
        self.f.http.owners={'gpu1':{'job_id':self.job}}
        self.f.http.jobs[self.job]=dict(self.terminal,state='running',resources_released=False,supervision_active=True)
        self.refused('previous_runner_job_unresolved')
        self.f.http.owners={}
        self.refused('previous_runner_job_unresolved')
        self.f.http.jobs[self.job]=dict(self.terminal,request_hash='f'*64)
        self.refused()

    def test_later_terminal_job_adds_positive_proof_and_second_lookup_appearance_refuses(self):
        original=self.write(self.legacy());self.f.http.jobs[self.job]=dict(self.terminal)
        self.f.restart().reconcile_runner_jobs()
        self.assertEqual(len(self.f.receipts()),2)
        self.assertEqual(self.receipt.read_bytes(),original)
        self.f.http.calls.clear();self.f.restart().reconcile_runner_jobs()
        self.assertEqual(self.f.http.calls,['/resources','/resources'])
        # Separate operation has only an absence receipt; appearance between
        # the two fresh GETs must refuse, even when its identity is exact.
        other,path=self.f.add(2);status=self.f.http.jobs.pop(other)
        self.f.restart().reconcile_runner_jobs()
        record=json.loads(path.read_bytes());pending=record['runner_jobs'][other]
        import hashlib
        key=hashlib.sha256(encode([self.f.slot.scope,record['request_id'],other,pending,'not_submitted'])).hexdigest()
        receipt=self.f.slot.store.path('dispatch-reconciliations/'+key+'.json')
        value=json.loads(receipt.read_bytes());value['proof']['owners']=value['proof'].pop('unrelated_owners');receipt.write_bytes(encode(value))
        raw=receipt.read_bytes();request=self.f.http.request;count=[]
        def appear(method,route):
            if route=='/jobs/'+other:
                self.assertEqual(method,'GET');self.f.http.calls.append(route);count.append(1)
                return (404,'application/json',b'{}') if len(count)==1 else (200,'application/json',encode(status))
            return request(method,route)
        self.f.http.request=appear
        with self.assertRaises(DispatchBlocked) as caught:self.f.restart().reconcile_runner_jobs()
        self.assertEqual(caught.exception.reason,'previous_runner_job_unresolved')
        self.assertEqual(receipt.read_bytes(),raw)

    def test_legacy_proof_never_replays_original_operation(self):
        original=self.write(self.legacy());slot=self.f.restart()
        operation=slot.begin('render-async','new-schedule');operation.finish({'ok':True})
        self.assertEqual(self.receipt.read_bytes(),original)
        self.assertEqual(self.operation.read_bytes(),self.operation_bytes)
        self.f.http.calls.clear()
        with self.assertRaises(CoordinationError):slot.begin('render-async',json.loads(self.operation_bytes)['request_id'])
        self.assertEqual(self.f.http.calls,[])
