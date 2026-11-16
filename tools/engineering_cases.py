"""Small multi-file Python projects; oracle tests stay outside Agent workspace."""
import random


def config_case(seed):
    rng=random.Random(seed);limit=rng.randrange(3,20);workers=rng.randrange(2,9)
    files={'config.py':'''def parse_bool(value):
    return bool(value)

def parse_limit(value):
    return int(value.rstrip("KMkm"))
''','server.py':'''from config import parse_bool,parse_limit
def load_config(values):
    return {"enabled":parse_bool(values.get("enabled","false")),"limit":parse_limit(values.get("limit","64")),"workers":int(values.get("workers") or "1")}
''','test_config.py':f'''import unittest
from server import load_config
from config import parse_bool,parse_limit
class ConfigTests(unittest.TestCase):
 def test_false(self): self.assertIs(parse_bool(' false '),False)
 def test_limit(self): self.assertEqual(parse_limit('{limit}k'),{limit*1024})
 def test_load(self): self.assertEqual(load_config({{'enabled':'yes','limit':'2M','workers':'{workers}'}}),{{'enabled':True,'limit':2097152,'workers':{workers}}})
 def test_bad(self):
  with self.assertRaises(ValueError): load_config({{'workers':'0'}})
'''}
    golden={'config.py':'''import re
def parse_bool(value):
    if type(value) is bool: return value
    if not isinstance(value,str): raise ValueError('boolean')
    text=value.strip().lower()
    if text in ('true','yes','1','on'): return True
    if text in ('false','no','0','off'): return False
    raise ValueError('boolean')
def parse_limit(value):
    if not isinstance(value,str): raise ValueError('limit')
    match=re.fullmatch(r'([0-9]+)([kKmM]?)',value.strip())
    if not match: raise ValueError('limit')
    return int(match[1])*{'':1,'k':1024,'m':1048576}[match[2].lower()]
''','server.py':'''from config import parse_bool,parse_limit
def load_config(values):
    raw=values.get('workers','1')
    if not isinstance(raw,str) or not raw.strip().isdigit(): raise ValueError('workers')
    workers=int(raw.strip())
    if workers<1 or workers>64: raise ValueError('workers')
    return {'enabled':parse_bool(values.get('enabled','false')),'limit':parse_limit(values.get('limit','64')),'workers':workers}
'''}
    extra='''import unittest
from config import parse_bool,parse_limit
from server import load_config
class Extra(unittest.TestCase):
 def test_truth(self):
  for text in ('TRUE',' yes ','1','On'): self.assertIs(parse_bool(text),True)
 def test_false(self):
  for text in (' FALSE ','no','0','OFF'): self.assertIs(parse_bool(text),False)
 def test_bool(self):
  self.assertIs(parse_bool(True),True);self.assertIs(parse_bool(False),False)
 def test_bad_bool(self):
  for value in ('maybe','',None,1):
   with self.assertRaises(ValueError): parse_bool(value)
 def test_sizes(self):
  for text,value in [('0',0),(' 7K ',7168),('3m',3145728),('91',91)]: self.assertEqual(parse_limit(text),value)
 def test_bad_size(self):
  for value in ('-1','1.5K','x','2G','1 K',None):
   with self.assertRaises(ValueError): parse_limit(value)
 def test_defaults(self): self.assertEqual(load_config({}),{'enabled':False,'limit':64,'workers':1})
 def test_worker_range(self):
  for value in ('0','65','-3','1.5','',None,True):
   with self.assertRaises(ValueError): load_config({'workers':value})
 def test_worker_valid(self): self.assertEqual(load_config({'workers':' 64 '})['workers'],64)
'''
    requirements='parse_bool接受bool原样返回，或去空白且不区分大小写的true/yes/1/on和false/no/0/off字符串，其余输入ValueError。parse_limit只接受去首尾空白的非负十进制整数字符串，可带K/M（大小写均可），单位分别1024/1048576；中间空格、小数、负数及其他类型均ValueError。load_config使用缺失默认enabled=false、limit=64、workers=1；workers必须是去首尾空白后仅十进制数字的字符串且在1..64，其他类型/空串/越界拒绝。保留接口与测试。'
    return {'family':'config','files':files,'golden':golden,'extra_test':extra,'requirements':requirements}


def logs_case(seed):
    rng=random.Random(seed);duration=rng.randrange(11,97)
    files={'events.py':'''def parse_event(line):
    service,duration,status=line.split(',')
    return service,int(duration),status
''','report.py':'''from events import parse_event
def summarize(lines,service=None):
    events=[parse_event(line) for line in lines if line.strip()]
    durations=[duration for name,duration,status in events if status=='ok']
    return {'count':len(events),'errors':0,'total_ms':sum(durations),'max_ms':max(durations)}
''','test_report.py':f'''import unittest
from events import parse_event
from report import summarize
class ReportTests(unittest.TestCase):
 def test_parse(self): self.assertEqual(parse_event(' api , {duration} ,ok'),('api',{duration},'ok'))
 def test_errors(self): self.assertEqual(summarize(['api,3,ok','api,5,error']),{{'count':2,'errors':1,'total_ms':8,'max_ms':5}})
 def test_filter(self): self.assertEqual(summarize(['api,3,ok','db,5,error'],'api')['count'],1)
 def test_empty(self): self.assertEqual(summarize([]),{{'count':0,'errors':0,'total_ms':0,'max_ms':0}})
'''}
    golden={'events.py':'''def parse_event(line):
    if not isinstance(line,str): raise ValueError('event')
    parts=[part.strip() for part in line.split(',')]
    if len(parts)!=3 or not parts[0] or not parts[1].isdigit() or parts[2] not in ('ok','error'): raise ValueError('event')
    return parts[0],int(parts[1]),parts[2]
''','report.py':'''from events import parse_event
def summarize(lines,service=None):
    events=[parse_event(line) for line in lines if line.strip()]
    events=[event for event in events if service is None or event[0]==service]
    durations=[event[1] for event in events]
    return {'count':len(events),'errors':sum(event[2]=='error' for event in events),'total_ms':sum(durations),'max_ms':max(durations,default=0)}
'''}
    extra='''import unittest
from events import parse_event
from report import summarize
class Extra(unittest.TestCase):
 def test_invalid(self):
  for text in ('a,-1,ok',',2,ok','a,2,bad','a,1.5,ok','a,3,ok,x'):
   with self.assertRaises(ValueError): parse_event(text)
 def test_zero(self): self.assertEqual(parse_event('a,0,error'),('a',0,'error'))
 def test_spaces(self): self.assertEqual(parse_event(' db , 13 , error '),('db',13,'error'))
 def test_no_match(self): self.assertEqual(summarize(['db,8,error'],'api'),{'count':0,'errors':0,'total_ms':0,'max_ms':0})
 def test_blank(self): self.assertEqual(summarize(['','  '])['count'],0)
 def test_every_duration(self): self.assertEqual(summarize(['a,7,error','a,2,ok','b,0,error']),{'count':3,'errors':2,'total_ms':9,'max_ms':7})
 def test_filter_error(self): self.assertEqual(summarize(['a,7,error','a,2,ok','b,9,error'],'a'),{'count':2,'errors':1,'total_ms':9,'max_ms':7})
 def test_invalid_unselected(self):
  with self.assertRaises(ValueError): summarize(['db,-1,error'],'api')
'''
    requirements='parse_event接收恰好3个逗号分隔字段，逐项strip，服务名非空，时长为非负整数字符串，状态仅ok或error；格式错误ValueError。summarize忽略空白行，先验证所有其他行再按service精确筛选（None表示全部）。count、errors、total_ms、max_ms均针对筛选后的所有状态，空集四项均0。保留接口与测试。'
    return {'family':'logs','files':files,'golden':golden,'extra_test':extra,'requirements':requirements}
