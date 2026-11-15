import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from agent_runtime.chat_store import ChatStore,history_messages
from agent_runtime.web import Controller,make_server
from agent_runtime.model_gateway import ModelGateway

class ChatTests(unittest.TestCase):
    def wait(self,controller,key):
        end=time.monotonic()+8
        while time.monotonic()<end:
            chat=controller.chat_detail(key)
            if chat['turns'][-1]['status']!='running':return chat
            time.sleep(.02)
        self.fail('Conversation did not settle')

    def payload(self,mode='document'):
        return {'conversation_id':'a'*32,'request_id':'b'*32,'mode':mode,'prompt':'Agent runtime shares context.'}

    def test_real_document_chat_restores_and_repeated_post_does_not_execute_again(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'AGENTRT_MODEL_BROKER_URL':''}):
            c=Controller(tmp,os.environ.get('AGENTRT_TEST_CGROUP'))
            first=c.submit_chat(self.payload());chat=self.wait(c,'a'*32)
            self.assertEqual(chat['turns'][0]['status'],'succeeded')
            self.assertIn('文档分析',chat['turns'][0]['answer'])
            second=c.submit_chat(self.payload());self.assertTrue(second['reused'])
            self.assertEqual(first['job_id'],second['job_id']);self.assertEqual(len(c.jobs),1)
            restored=Controller(tmp).chat_detail('a'*32)
            self.assertEqual(restored['turns'][0]['answer'],chat['turns'][0]['answer'])
            with self.assertRaises(ValueError):c.submit_chat({**self.payload(),'prompt':'different'})

    def test_followup_reaches_model_as_authoritative_prior_messages(self):
        received=[]
        class Client:
            timeout=2;model='offline'
            def complete(self,messages,*args):received.append(messages);return {'content':'remembered answer','usage':{}}
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'AGENTRT_MODEL_BROKER_URL':''}):
            c=Controller(tmp)
            with patch.object(c,'model_info',return_value={'configured':True,'model':'offline'}),\
                 patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda p:ModelGateway(Client(),p)):
                c.submit_chat(self.payload('llm'));self.wait(c,'a'*32)
                c.submit_chat({**self.payload('llm'),'request_id':'c'*32,'prompt':'What did I ask?'});self.wait(c,'a'*32)
            self.assertEqual([m['role'] for m in received[1]],['user','assistant','user'])
            self.assertEqual(received[1][1]['content'],'remembered answer')
            self.assertEqual(received[1][-1]['content'],'What did I ask?')

    def test_uncertain_intent_is_not_replayed_and_restart_marks_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ChatStore(tmp)
            store.intent('a'*32,'b'*32,'hello','llm','pricing',{'calls':4,'output_tokens':4096})
            with self.assertRaisesRegex(ValueError,'状态尚未确认'):
                store.intent('a'*32,'b'*32,'hello','llm','pricing',{'calls':4,'output_tokens':4096})
            self.assertEqual(ChatStore(tmp).read('a'*32)['turns'][0]['status'],'interrupted')
            if os.name=='posix':self.assertEqual((Path(tmp)/('a'*32+'.json')).stat().st_mode&0o777,0o600)

    def test_history_is_bounded_and_excludes_failed_turns(self):
        turns=[{'prompt':'x'*1000,'answer':'y'*1000,'status':'succeeded','mode':'llm'} for _ in range(20)]
        turns.insert(19,{'prompt':'private failure','answer':'bad','status':'failed','mode':'llm'})
        messages=history_messages({'turns':turns+[{}]})
        self.assertLessEqual(len(messages),12)
        self.assertLessEqual(len(json.dumps(messages,ensure_ascii=False).encode()),24576)
        self.assertNotIn('private failure',str(messages))

    def test_corrupt_conversation_is_skipped_without_breaking_console_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'conversations';p.mkdir()
            (p/('a'*32+'.json')).write_text('[]')
            c=Controller(tmp)
            self.assertEqual(c.chats.listing(),[])
            with self.assertRaises(ValueError):c.chats.read('a'*32)

    def test_cancelled_chat_converges_without_executing_model(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'AGENTRT_MODEL_BROKER_URL':''}):
            c=Controller(tmp)
            with patch('agent_runtime.scenarios.tasks_for',return_value=[{'id':'slow','action':'sleep','seconds':5}]):
                submitted=c.submit_chat(self.payload())
            c.cancel_job(submitted['job_id']);chat=self.wait(c,'a'*32)
            self.assertEqual(chat['turns'][0]['status'],'cancelled')
            self.assertIn('停止',chat['turns'][0]['answer'])

    def test_http_chat_console_routes_are_separate_and_keep_authentication(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=Controller(tmp);server=make_server(c,port=0,proxy_token='offline-fixture')
            threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                base='http://127.0.0.1:'+str(server.server_port)
                def get(path):return urlopen(Request(base+path,headers={'X-AgentRT-Proxy-Token':'offline-fixture'}),timeout=3).read().decode()
                self.assertIn('有什么想一起完成',get('/'))
                self.assertIn('工作流资源分配',get('/console'))
                self.assertIn('<base href="../">',get('/console/'))
                self.assertNotIn('machine',json.loads(get('/api/chat/status')))
                for path in ('/','/console','/api/chat/conversations'):
                    with self.assertRaises(HTTPError) as e:urlopen(base+path,timeout=3)
                    self.assertEqual(e.exception.code,403)
                with self.assertRaises(HTTPError) as e:
                    urlopen(Request(base+'/api/chat/turns',data=json.dumps(self.payload()).encode(),headers={'X-AgentRT-Proxy-Token':'offline-fixture'}),timeout=3)
                self.assertEqual(e.exception.code,403)
            finally:server.shutdown();server.server_close()
