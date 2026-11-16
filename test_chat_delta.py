"""HTTP polling sends only the active tail, preserving protected full-history fallback."""
import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from agent_runtime.web import Controller,make_server


class ChatDeltaTests(unittest.TestCase):
    def test_tail_reduces_wire_bytes_and_unknown_identity_falls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            controller=Controller(folder)
            for key,answer in [('b'*32,'historical answer '*3000),('c'*32,'latest answer')]:
                controller.chats.intent('a'*32,key,'synthetic request','llm','pricing',{'calls':0,'output_tokens':0})
                controller.chats.update('a'*32,key,status='succeeded',answer=answer)
            server=make_server(controller,port=0,proxy_token='offline-fixture')
            threading.Thread(target=server.serve_forever,daemon=True).start()
            base='http://127.0.0.1:'+str(server.server_port)+'/api/chat/conversations/'+'a'*32
            def get(suffix):
                with urlopen(Request(base+suffix,headers={'X-AgentRT-Proxy-Token':'offline-fixture'}),timeout=3) as response:return response.read()
            try:
                full=get('');tail=get('?tail='+'c'*32);value=json.loads(tail)
                self.assertEqual(value['turn_offset'],1)
                self.assertEqual(len(value['turns']),1)
                self.assertEqual(value['turns'][0]['request_id'],'c'*32)
                self.assertEqual(value['turns'][0]['answer'],'latest answer')
                self.assertLess(len(tail),len(full)*.1)
                self.assertNotIn('turn_offset',json.loads(get('?tail='+'d'*32)))
                self.assertEqual(json.loads(full),json.loads(get('?tail='+'d'*32)))
                with self.assertRaises(HTTPError) as error:urlopen(base+'?tail='+'c'*32,timeout=3)
                self.assertEqual(error.exception.code,403)
                with self.assertRaises(HTTPError) as error:get('?tail=invalid')
                self.assertEqual(error.exception.code,400)
            finally:server.shutdown();server.server_close()
