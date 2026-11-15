import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request,urlopen
from agent_runtime.chat_store import ChatStore
from agent_runtime.web import Controller,make_server


class ChatEfficiencyTests(unittest.TestCase):
    def test_unchanged_listing_uses_metadata_and_invalidates_after_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ChatStore(tmp);key='a'*32;request='b'*32
            store.intent(key,request,'hello','auto','pricing',{})
            store.update(key,request,status='succeeded',answer='x'*10000,job_id='fixture')
            with patch.object(store,'read',side_effect=AssertionError('must not reparse unchanged history')):
                self.assertEqual(store.listing()[0]['title'],'hello')
            value=json.loads((Path(tmp)/(key+'.json')).read_text());value['title']='changed'
            (Path(tmp)/(key+'.json')).write_text(json.dumps(value))
            self.assertEqual(store.listing()[0]['title'],'changed')

    def test_unchanged_conversation_poll_returns_revision_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=Controller(tmp);key='a'*32;c.chats.intent(key,'b'*32,'fixture','auto','pricing',{})
            c.chats.update(key,'b'*32,status='succeeded',answer='completed',job_id='fixture')
            server=make_server(c,port=0,proxy_token='fixture-proxy')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                base='http://127.0.0.1:'+str(server.server_port)+'/api/chat/conversations/'+key
                def get(url):
                    with urlopen(Request(url,headers={'X-AgentRT-Proxy-Token':'fixture-proxy'}),timeout=3) as f:return json.load(f)
                chat=get(base)
                with patch.object(c,'chat_detail',side_effect=AssertionError('must not read unchanged full conversation')):
                    self.assertTrue(get(base+'?after='+chat['revision'])['not_modified'])
                c.chats.update(key,'b'*32,answer='new answer')
                self.assertEqual(get(base+'?after='+chat['revision'])['turns'][-1]['answer'],'new answer')
            finally:server.shutdown();server.server_close()
