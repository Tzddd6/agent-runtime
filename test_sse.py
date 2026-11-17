import io,json,unittest
from agent_runtime.sse import payloads


class SSETests(unittest.TestCase):
 def test_utf8_split_crlf_bom_comments_and_multiline(self):
  text='\ufeff: heartbeat\r\nevent: message\r\ndata: {"type":\r\ndata: "fixture", "text":"公式\\n代码中文"}\r\n\r\n'
  class OneByte(io.BytesIO):
   def read1(self,n):return self.read(1)
  rows=list(payloads(OneByte(text.encode()),4096))
  self.assertEqual(json.loads(rows[0]),{'type':'fixture','text':'公式\n代码中文'})
 def test_cr_only_and_lf_events(self):
  self.assertEqual(list(payloads(io.BytesIO(b'data: one\r\rdata: two\n\n'),128)),['one','two'])
 def test_unterminated_event_never_becomes_receipt(self):
  self.assertEqual(list(payloads(io.BytesIO(b'data: {"status":"completed"}\n'),128)),[])
 def test_wire_and_event_limits(self):
  for value in (b'data: '+b'a'*129+b'\n\n',b':\n\n'*400):
   with self.assertRaises(ValueError):list(payloads(io.BytesIO(value),128))
 def test_invalid_utf8_rejected(self):
  with self.assertRaises(UnicodeError):list(payloads(io.BytesIO(b'data: \xff\n\n'),128))

if __name__=='__main__':unittest.main()
