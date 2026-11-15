import concurrent.futures
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
from agent_runtime.database import RuntimeDatabase
from agent_runtime.knowledge import KnowledgeStore,terms,chunks
from agent_runtime.web import Controller,make_server
from agent_runtime.model_gateway import ModelGateway


class KnowledgeTests(unittest.TestCase):
    def store(self,root):return KnowledgeStore(RuntimeDatabase(Path(root)/'db'))

    def test_bilingual_search_and_no_unrelated_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb=self.store(tmp)
            kb.save({'title':'资源规范','body':'cgroup 限制内存。交换空间使用 memory.swap.max=0。'})
            kb.save({'title':'水果','body':'苹果和西瓜很甜。'})
            self.assertEqual(kb.search('如何限制内存')[0]['title'],'资源规范')
            self.assertIn('memory.swap.max',kb.search('memory swap')[0]['body'])
            self.assertEqual(kb.search('astronomy quasars'),[])
            self.assertEqual(kb.search('" OR 1=1; --'),[])
            self.assertEqual(kb.listing()['engine'],'sqlite_fts5_bilingual')

    def test_long_documents_keep_exact_excerpt_and_line_ranges(self):
        text=''.join('第'+str(i)+'行：资源与并发预算。\n' for i in range(300))
        parts=chunks(text)
        self.assertGreater(len(parts),1)
        for _,first,last,body in parts:
            self.assertIn(body,text);self.assertLessEqual(len(body),1200)
            self.assertEqual(last-first+1,body.count('\n')+(0 if body.endswith('\n') else 1))
        self.assertIn('资源',terms('资源管理'));self.assertIn('memory',terms('MEMORY'))

    def test_replace_reindexes_and_stale_updates_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb=self.store(tmp);a=kb.save({'title':'one','body':'alpha baseline'})
            self.assertTrue(kb.save({'id':a['id'],'title':'one','body':'alpha baseline'})['reused'])
            b=kb.save({'id':a['id'],'expected_sha256':a['sha256'],'title':'two','body':'beta revised'})
            self.assertEqual(b['revision'],2);self.assertEqual(kb.search('alpha'),[])
            self.assertEqual(kb.search('beta')[0]['revision'],2)
            with self.assertRaisesRegex(ValueError,'已更新'):
                kb.save({'id':a['id'],'expected_sha256':a['sha256'],'title':'stale','body':'gamma'})
            self.assertEqual(kb.read(a['id'])['body'],'beta revised')
            renamed=kb.save({'id':a['id'],'expected_sha256':b['sha256'],'title':'renamed','body':'beta revised'})
            self.assertNotEqual(renamed['sha256'],b['sha256'])

    def test_delete_cleans_index_but_captured_source_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb=self.store(tmp);a=kb.save({'title':'spec','body':'budget baseline'})
            snapshot=kb.context('budget');kb.remove({'id':a['id'],'expected_sha256':a['sha256']})
            self.assertEqual(kb.search('budget'),[]);self.assertEqual(kb.listing()['chunks'],0)
            self.assertEqual(snapshot['sources'][0]['body'],'budget baseline')
            self.assertFalse(kb.remove({'id':a['id']})['removed'])
            with self.assertRaises(ValueError):kb.save({'id':a['id'],'expected_sha256':a['sha256'],'title':'x','body':'y'})

    def test_storage_survives_reopen_with_bounded_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb=self.store(tmp)
            for i in range(8):kb.save({'title':str(i),'body':'资源内存 memory '+str(i)+'\n'+'资源限制。'*600})
            context=self.store(tmp).context('资源 memory')
            self.assertLessEqual(len(context['sources']),4)
            self.assertLessEqual(context['excerpt_chars'],4800)
            self.assertEqual(context['sources'][0]['citation'],'K1')
            self.assertIn('不可信',context['instruction'])

    def test_reject_secrets_bad_ids_and_oversized_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb=self.store(tmp)
            for value in [{'title':'x','body':'sk-'+'A'*24},{'title':'x','body':'-----BEGIN PRIVATE KEY-----'},
                          {'title':'x','body':'x'*(256*1024+1)},{'title':'x\n','body':'y'},
                          {'id':'../../secret','title':'x','body':'y'},{'title':'x','body':'\ud800'}]:
                with self.assertRaises(ValueError):kb.save(value)
            self.assertEqual(kb.listing()['documents'],[])

    def test_concurrent_compare_and_swap_has_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb=self.store(tmp);a=kb.save({'title':'base','body':'base'});barrier=threading.Barrier(2)
            def update(i):
                barrier.wait()
                try:kb.save({'id':a['id'],'expected_sha256':a['sha256'],'title':'new'+str(i),'body':'updated'+str(i)});return True
                except ValueError:return False
            with concurrent.futures.ThreadPoolExecutor(2) as pool:self.assertEqual(sum(pool.map(update,range(2))),1)

    def test_http_read_write_auth_csrf_and_index_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=Controller(tmp);server=make_server(c,port=0,proxy_token='offline-test');threading.Thread(target=server.serve_forever,daemon=True).start()
            base='http://127.0.0.1:'+str(server.server_port)
            def request(path,value=None,csrf=True):
                headers={'X-AgentRT-Proxy-Token':'offline-test'}
                if csrf:headers['X-AgentRT-CSRF']=c.csrf
                raw=None if value is None else json.dumps(value).encode()
                return json.load(urlopen(Request(base+path,data=raw,headers=headers),timeout=3))
            try:
                with self.assertRaises(HTTPError) as e:urlopen(base+'/api/knowledge',timeout=3)
                self.assertEqual(e.exception.code,403)
                with self.assertRaises(HTTPError) as e:request('/api/knowledge/save',{'title':'test','body':'abc'},False)
                self.assertEqual(e.exception.code,403)
                a=request('/api/knowledge/save',{'title':'test','body':'memory limit 128MiB'})
                self.assertEqual(len(request('/api/knowledge')['documents']),1)
                self.assertEqual(request('/api/knowledge/search?q=memory')['sources'][0]['title'],'test')
                self.assertEqual(request('/api/knowledge/'+a['id'])['body'],'memory limit 128MiB')
                request('/api/knowledge/remove',{'id':a['id'],'expected_sha256':a['sha256']})
                self.assertEqual(request('/api/knowledge/search?q=memory')['sources'],[])
            finally:server.shutdown();server.server_close()

    def test_chat_retrieves_once_persists_sources_and_honors_off_switch(self):
        received=[]
        class Client:
            timeout=2;model='offline'
            def complete(self,messages,*args):
                received.append(messages);return {'content':'{"route":"direct","answer":"配额是173MiB [K1]"}','usage':{}}
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'AGENTRT_MODEL_BROKER_URL':''}):
            c=Controller(tmp,os.environ.get('AGENTRT_TEST_CGROUP'))
            source=c.knowledge.save({'title':'项目规范','body':'memory配额固定为173MiB。'})
            value={'conversation_id':'a'*32,'request_id':'b'*32,'prompt':'memory配额是多少','mode':'auto'}
            with patch.object(c,'model_info',return_value={'configured':True,'model':'offline'}),patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda p:ModelGateway(Client(),p)):
                result=c.submit_chat(value)
                end=time.monotonic()+8
                while time.monotonic()<end:
                    chat=c.chat_detail('a'*32)
                    if chat['turns'][-1]['status']!='running':break
                    time.sleep(.03)
                self.assertEqual(chat['turns'][-1]['status'],'succeeded')
                self.assertIn('173MiB',str(received[0]));self.assertEqual(chat['turns'][0]['knowledge']['sources'][0]['document_id'],source['id'])
                c.knowledge.remove({'id':source['id'],'expected_sha256':source['sha256']})
                self.assertEqual(c.chat_detail('a'*32)['turns'][0]['knowledge']['sources'][0]['body'],'memory配额固定为173MiB。')
                self.assertTrue(c.submit_chat(value)['reused']);self.assertEqual(len(received),1)
                with self.assertRaises(ValueError):c.submit_chat({**value,'use_knowledge':False})
                c.submit_chat({**value,'request_id':'c'*32,'use_knowledge':False})
                end=time.monotonic()+8
                while time.monotonic()<end and c.chat_detail('a'*32)['turns'][-1]['status']=='running':time.sleep(.03)
                self.assertNotIn('knowledge',c.chat_detail('a'*32)['turns'][-1])


if __name__=='__main__':unittest.main()
