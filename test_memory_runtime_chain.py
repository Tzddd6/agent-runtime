"""Real HTTP -> Controller -> Runtime -> Worker -> Gateway, fixed provider only."""
import base64
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from agent_runtime.web import Controller, make_server
from agent_runtime.model_gateway import ModelGateway
from agent_runtime.memory_repository import MemoryRepository
from agent_runtime.runtime import run
from agent_runtime.ipc import Broker
from agent_runtime.context import ContextStore


class FixedProvider:
    model='fixed-fixture'; timeout=5
    def __init__(self):self.calls=[]
    def complete(self,messages,max_tokens=256):
        self.calls.append(json.loads(json.dumps(messages)))
        return {'content':'fixture-ok','usage':{'prompt_tokens':1,'completion_tokens':1}}


class RuntimeMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ,{'AGENTRT_MODEL_BROKER_URL':''}).start()
        patch.object(Controller,'model_info',return_value={'configured':True,'model':'fixed-fixture','available':True}).start()
        self.provider=FixedProvider()
        patch.object(ModelGateway,'from_env',side_effect=lambda log:ModelGateway(self.provider,log)).start()
        self.c=Controller(self.root,os.environ.get('AGENTRT_TEST_CGROUP'),user_identity='alice')
        self.server=make_server(self.c,port=0,proxy_token='synthetic-test-proxy',tenant_routing=True,legacy_user='alice')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.base='http://127.0.0.1:'+str(self.server.server_port)

    def request(self,user,path,value=None,csrf=None,token='synthetic-test-proxy'):
        headers={'X-AgentRT-Proxy-Token':token,'X-AgentRT-User':user}
        if value is not None:
            if csrf is None:csrf=self.request(user,'/api/chat/status')[1]['csrf']
            headers.update({'X-AgentRT-CSRF':csrf,'Content-Type':'application/json'})
        req=Request(self.base+path,data=json.dumps(value).encode() if value is not None else None,headers=headers)
        try:
            with urlopen(req,timeout=10) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)

    def chat(self,user,mode='llm',key=None):
        key=key or uuid.uuid4().hex
        code,value=self.request(user,'/api/chat/turns',{'conversation_id':key,'request_id':uuid.uuid4().hex,'prompt':'fixed request','mode':mode})
        self.assertEqual(code,202,value)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            code,chat=self.request(user,'/api/chat/conversations/'+key)
            if code==200 and chat['turns'][-1]['status']!='running':return value,chat
            time.sleep(.02)
        self.fail('Workflow did not finish')

    def test_http_memory_injected_into_actual_worker_gateway_and_cross_user_denied(self):
        self.assertEqual(self.request('alice','/api/memory/save',{'content':'ALICE-PRIVATE-CANARY'})[0],201)
        job,chat=self.chat('alice')
        self.assertEqual(chat['turns'][-1]['status'],'succeeded')
        self.assertIn('ALICE-PRIVATE-CANARY',json.dumps(self.provider.calls[-1]))
        self.assertEqual(self.request('bob','/api/memory')[1]['memories'],[])
        self.assertEqual(self.request('bob','/api/jobs/'+job['job_id'])[0],404)
        self.assertEqual(self.request('bob','/api/chat/conversations')[1]['conversations'],[])
        self.assertNotEqual(self.request('bob','/api/chat/conversations/'+job['conversation_id'])[0],200)
        _,chat=self.chat('bob',key=job['conversation_id'])
        self.assertEqual(chat['turns'][-1]['status'],'succeeded')
        self.assertNotIn('ALICE-PRIVATE-CANARY',json.dumps(self.provider.calls[-1]))

    def test_team_private_excluded_shared_visible_for_actual_four_agents(self):
        self.request('alice','/api/memory/save',{'content':'PRIVATE-TEAM-CANARY','policy':'private'})
        self.request('alice','/api/memory/save',{'content':'SHARED-TEAM-CANARY','policy':'shared'})
        _,chat=self.chat('alice','team')
        self.assertEqual(chat['turns'][-1]['status'],'succeeded')
        self.assertEqual(len(self.provider.calls),4)
        for messages in self.provider.calls:
            text=json.dumps(messages)
            self.assertNotIn('PRIVATE-TEAM-CANARY',text)
            self.assertIn('SHARED-TEAM-CANARY',text)

    def test_missing_identity_bad_proxy_and_foreign_csrf_rejected(self):
        self.assertEqual(self.request('','/api/chat/status')[0],403)
        self.assertEqual(self.request('alice','/api/chat/status',token='wrong')[0],403)
        alice=self.request('alice','/api/chat/status')[1]['csrf']
        self.assertEqual(self.request('bob','/api/memory/save',{'content':'x'},csrf=alice)[0],403)
        self.assertEqual(self.request('alice','/api/memory/save',{'content':'x','user_id':'bob'})[0],400)

    def test_http_budget_bound_at_actual_provider_boundary(self):
        for i in range(4):self.request('alice','/api/memory/save',{'content':str(i)+'中'*1300})
        self.chat('alice')
        blocks=[m['content'] for m in self.provider.calls[-1] if m['content'].startswith('以下是经授权')]
        self.assertEqual(len(blocks),1)
        self.assertLessEqual(len(blocks[0].encode()),8192)
        self.assertIn('fixed request',json.dumps(self.provider.calls[-1]))

    def test_database_failure_blocks_provider_and_manual_restore_recovers(self):
        memory=self.c.memory
        memory.put('assistant','RESTORE-CANARY')
        with memory.connect() as db:db.execute('ALTER TABLE notes RENAME TO unavailable_notes')
        _,chat=self.chat('alice')
        self.assertEqual(chat['turns'][-1]['status'],'failed')
        self.assertEqual(self.provider.calls,[])
        with memory.connect() as db:db.execute('ALTER TABLE unavailable_notes RENAME TO notes')
        self.chat('alice')
        self.assertEqual(len(self.provider.calls),1)
        self.assertIn('RESTORE-CANARY',json.dumps(self.provider.calls[-1]))

    def test_committed_memory_survives_controller_restart_and_removal(self):
        _,saved=self.request('alice','/api/memory/save',{'content':'DURABLE-CANARY'})
        restored=Controller(self.root,user_identity='alice')
        self.assertIn('DURABLE-CANARY',json.dumps(restored.memory.inject('assistant',[])[0]))
        self.request('alice','/api/memory/remove',{'id':saved['id']})
        self.chat('alice')
        self.assertNotIn('DURABLE-CANARY',json.dumps(self.provider.calls[-1]))

    def test_global_resource_admission_not_bypassed_by_tenant(self):
        with self.c.lock:self.c.jobs['fake']={'status':'running'}
        try:self.assertEqual(self.request('bob','/api/jobs',{'preset':'dynamic'})[0],400)
        finally:
            with self.c.lock:self.c.jobs.pop('fake')

    def test_capability_binds_agent_and_revocation(self):
        store=ContextStore(self.root/'test-context')
        broker=Broker(store,['owner','peer'],memory_repository=self.c.memory)
        self.addCleanup(broker.close)
        owner=broker.issue('owner');peer=broker.issue('peer')
        broker.dispatch(owner,{'op':'memory_write','content':'OWNER-PRIVATE'})
        self.assertEqual(broker.dispatch(peer,{'op':'memory_read'})['memories'],[])
        with self.assertRaises(ValueError):broker.dispatch(peer,{'op':'memory_read','agent':'owner'})
        broker.revoke(owner)
        with self.assertRaises(PermissionError):broker.dispatch(owner,{'op':'memory_read'})

    def test_worker_write_and_other_agent_read_use_runtime_ipc(self):
        tasks=[{'id':'write','agent':'writer','action':'memory_write','content':'WORKER-SHARED','policy':'shared'},
               {'id':'read','agent':'reader','action':'memory_read','depends_on':['write']}]
        states,events=run(tasks,output_dir=self.root/'workflow',quiet=True,
                          cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'),memory_repository=self.c.memory)
        self.assertEqual(states,{'write':'succeeded','read':'succeeded'},events)
        value=next(e['result']['result'] for e in events if e['task_id']=='read' and e['state']=='succeeded')
        self.assertIn('WORKER-SHARED',json.dumps(value))

    def test_attachment_database_partition_isolated(self):
        upload={'conversation_id':'a'*32,'name':'note.txt','data':base64.b64encode(b'ALICE-UPLOAD').decode()}
        code,value=self.request('alice','/api/attachments',upload)
        self.assertEqual(code,201,value)
        code,value=self.request('bob','/api/chat/turns',{'conversation_id':'a'*32,'request_id':'b'*32,'mode':'document','prompt':'text','attachments':[value['id']]})
        self.assertEqual(code,400,value)

    def test_killed_writer_rolls_back_uncommitted_transaction(self):
        self.c.memory.put('assistant','COMMITTED-CANARY')
        script="import sqlite3,sys,time; d=sqlite3.connect(sys.argv[1]); d.execute(\"INSERT INTO notes VALUES ('uncommitted','alice','assistant','private','UNCOMMITTED',0)\"); print('ready',flush=True); time.sleep(10)"
        child=subprocess.Popen([sys.executable,'-u','-c',script,str(self.c.memory.path)],stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(),'ready')
            child.kill();child.wait(timeout=3)
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=3)
            child.stdout.close()
        restored=MemoryRepository(self.c.memory.root,'alice')
        text=json.dumps(restored.listing('assistant'))
        self.assertIn('COMMITTED-CANARY',text);self.assertNotIn('UNCOMMITTED',text)

    def test_parallel_commits_and_secret_rejection(self):
        errors=[]
        def write(i):
            try:self.c.memory.put('assistant','parallel-'+str(i))
            except Exception as exc:errors.append(type(exc).__name__)
        workers=[threading.Thread(target=write,args=(i,)) for i in range(16)]
        for worker in workers:worker.start()
        for worker in workers:worker.join(timeout=5)
        self.assertFalse(any(w.is_alive() for w in workers));self.assertEqual(errors,[])
        self.assertEqual(len(self.c.memory.listing('assistant')),16)
        with self.assertRaises(ValueError):self.c.memory.put('assistant','sk-'+'x'*24)

    def test_corrupt_context_object_blocks_provider_then_rebuild_recovers(self):
        self.c.memory.put('assistant','INTEGRITY-CANARY')
        rows,_=self.c.memory.select('assistant');path=self.c.memory.context.store.root/rows[0]['ref']
        good=path.read_bytes();path.write_bytes(b'corrupt')
        _,chat=self.chat('alice')
        self.assertEqual(chat['turns'][-1]['status'],'failed');self.assertEqual(self.provider.calls,[])
        path.write_bytes(good)
        self.chat('alice');self.assertEqual(len(self.provider.calls),1)

    def test_http_restart_restores_users_and_does_not_replay_completed_job(self):
        self.request('alice','/api/memory/save',{'content':'ALICE-RESTART'})
        self.request('bob','/api/memory/save',{'content':'BOB-RESTART'})
        job,_=self.chat('alice');calls=len(self.provider.calls)
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)
        restored=Controller(self.root,os.environ.get('AGENTRT_TEST_CGROUP'),user_identity='alice')
        self.server=make_server(restored,port=0,proxy_token='synthetic-test-proxy',tenant_routing=True,legacy_user='alice')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.assertIn('ALICE-RESTART',json.dumps(self.request('alice','/api/memory')[1]))
        self.assertNotIn('ALICE-RESTART',json.dumps(self.request('bob','/api/memory')[1]))
        self.assertIn('BOB-RESTART',json.dumps(self.request('bob','/api/memory')[1]))
        self.assertEqual(self.request('alice','/api/jobs/'+job['job_id'])[1]['status'],'succeeded')
        self.assertEqual(len(self.provider.calls),calls)

    def test_eight_user_owner_policy_combinations_at_actual_gateway(self):
        for same_user in (False,True):
            for same_agent in (False,True):
                for shared in (False,True):
                    with self.subTest(same_user=same_user,same_agent=same_agent,shared=shared):
                        folder=self.root/uuid.uuid4().hex
                        alice=MemoryRepository(folder,'alice')
                        alice.put('owner','MATRIX-CANARY','shared' if shared else 'private')
                        repo=alice if same_user else MemoryRepository(folder,'bob')
                        gateway=ModelGateway(self.provider,self.root/(uuid.uuid4().hex+'.jsonl'))
                        gateway.memory_repository=repo
                        token=gateway.issue('owner' if same_agent else 'peer')
                        try:
                            request=Request(gateway.url,data=json.dumps({'messages':[{'role':'user','content':'fixture'}],
                                'max_tokens':256,'agent':'owner','user_id':'alice'}).encode(),headers={'Authorization':'Bearer '+token})
                            with urlopen(request,timeout=5) as response:self.assertEqual(response.status,200)
                            allowed=same_user and (same_agent or shared)
                            self.assertEqual('MATRIX-CANARY' in json.dumps(self.provider.calls[-1]),allowed)
                        finally:gateway.close()


if __name__=='__main__':unittest.main()
