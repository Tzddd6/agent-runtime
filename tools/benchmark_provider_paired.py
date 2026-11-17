"""Bounded, paired same-model comparison. Offline by default; no key reads.

Direct and Runtime share one pinned RAM broker and identical provider requests.
This measures Runtime overhead above a common secure transport, not raw HTTP.
"""
import argparse,ast,hashlib,json,os,statistics,tempfile,time
from pathlib import Path

PRICE_SOURCE='https://developers.openai.com/api/docs/models/gpt-4.1-mini'
MODEL='gpt-4.1-mini'

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def message(text):return [{'role':'system','content':'Return only the requested JSON object, without markdown or explanation.'},{'role':'user','content':text}]

def cases():
 return [
  {'id':'structured','messages':message('Use these definitions: cpu.max=20000 100000 is a group CPU limit; memory.max=209715200 and memory.swap.max=0. Return JSON with cpu_cores (number), memory_bytes (integer), swap_bytes (integer), reservation_equals_usage (boolean false).'),'expected':{'cpu_cores':.2,'memory_bytes':209715200,'swap_bytes':0,'reservation_equals_usage':False}},
  {'id':'repair','messages':message('Repair refund_cents(paid_cents,returned_qty,total_qty), currently returning paid_cents*(returned_qty//total_qty). Valid inputs: nonnegative integer paid_cents; integer total_qty>0; 0<=returned_qty<=total_qty. Refund rounds down only after multiplication. Return JSON {"expression":"a Python arithmetic expression using only these three variable names, integer constants, +,-,*,// and parentheses"}. Do not return a function.' )},
  {'id':'tool_chain','messages':message('To get inventory of SKU-17, request the available inventory.lookup tool. Return exactly JSON {"tool":"inventory.lookup","arguments":{"sku":"SKU-17"}}.'),'tool_request':{'tool':'inventory.lookup','arguments':{'sku':'SKU-17'}},'tool_result':{'sku':'SKU-17','stock':17,'reserved':5},'expected':{'sku':'SKU-17','available':12}}
 ]

def expression(node,values):
 if isinstance(node,ast.Expression):return expression(node.body,values)
 if isinstance(node,ast.Name) and node.id in values:return values[node.id]
 if isinstance(node,ast.Constant) and type(node.value) is int and abs(node.value)<=100000:return node.value
 if isinstance(node,ast.BinOp) and isinstance(node.op,(ast.Add,ast.Sub,ast.Mult,ast.FloorDiv)):
  a,b=expression(node.left,values),expression(node.right,values)
  if isinstance(node.op,ast.Add):return a+b
  if isinstance(node.op,ast.Sub):return a-b
  if isinstance(node.op,ast.Mult):return a*b
  return a//b
 raise ValueError('Expression outside safe arithmetic grammar')

def grade(case,text):
 try:
  value=json.loads(text)
  if case['id']!='repair':return digest(value)==digest(case['expected'])
  if not isinstance(value,dict) or set(value)!={'expression'} or not isinstance(value['expression'],str) or len(value['expression'])>200:return False
  tree=ast.parse(value['expression'],mode='eval')
  if len(list(ast.walk(tree)))>48:return False
  for paid in (0,1,99,101,10003):
   for total in (1,3,7):
    for returned in (0,1,total):
     if expression(tree,{'paid_cents':paid,'returned_qty':returned,'total_qty':total})!=paid*returned//total:return False
  return True
 except (ValueError,TypeError,KeyError,SyntaxError,ArithmeticError):return False

class Ledger:
 def __init__(self,path,limit=.05,*,input_price=.4,output_price=1.6,price_source=PRICE_SOURCE):
  self.path=Path(path);self.limit=limit;self.reserved=0.;self.rows=[];self.input_price=input_price;self.output_price=output_price;self.price_source=price_source
  if self.path.exists():raise ValueError('Use a new ledger; never replay a partial live run')
 def admit(self,messages,output):
  # Upper-bound this fixed UTF-8 text fixture, including chat envelope overhead.
  tokens=len(json.dumps(messages,ensure_ascii=False).encode())+128*len(messages)+256
  cost=(tokens*self.input_price+output*self.output_price)/1e6
  if len(self.rows)>=16 or self.reserved+cost>self.limit:raise ValueError('Live spend/call reservation limit reached')
  self.reserved+=cost;row={'reserved_usd':cost,'request_sha256':digest(messages),'max_completion_tokens':output,'state':'admitted'};self.rows.append(row);self.flush();return row
 def flush(self):
  with self.path.open('w') as f:
   json.dump({'price_source':self.price_source,'currency':'USD','limit':self.limit,'reserved':self.reserved,'calls':self.rows},f,indent=2);f.flush();os.fsync(f.fileno())

class Measured:
 def __init__(self,client,ledger):self.client=client;self.model=client.model;self.timeout=client.timeout;self.ledger=ledger;self.records=[]
 def complete(self,messages,max_tokens):
  row=self.ledger.admit(messages,max_tokens);start=time.monotonic()
  try:
   result=self.client.complete(messages,max_tokens)
   if result.get('finish_reason')!='stop':raise ValueError('Incomplete output; no automatic retry')
   if not isinstance(result.get('usage'),dict) or not all(type(result['usage'].get(k)) is int and result['usage'][k]>=0 for k in ('prompt_tokens','completion_tokens')):raise ValueError('Missing provider usage')
   actual=(result['usage']['prompt_tokens']*self.ledger.input_price+result['usage']['completion_tokens']*self.ledger.output_price)/1e6
   if actual>row['reserved_usd']:
    self.ledger.reserved+=actual-row['reserved_usd'];row['reserved_usd']=actual
   row['reported_nominal_usd']=actual
   row.update(state='succeeded',usage=result['usage'],response_model=result.get('model'),seconds=time.monotonic()-start,provider_seconds=result.get('latency_seconds'),transport_timings=result.get('transport_timings',{}))
   self.records.append(row.copy());return result
  except BaseException:
   row.update(state='failed_billing_unknown',seconds=time.monotonic()-start);raise
  finally:self.ledger.flush()

class Fixture:
 model=MODEL;timeout=5
 def complete(self,messages,max_tokens):
  text=messages[-1]['content']
  if 'observed inventory' in text:content={'sku':'SKU-17','available':12}
  elif 'inventory.lookup' in text:content={'tool':'inventory.lookup','arguments':{'sku':'SKU-17'}}
  elif 'refund_cents' in text:content={'expression':'paid_cents*returned_qty//total_qty'}
  else:content={'cpu_cores':.2,'memory_bytes':209715200,'swap_bytes':0,'reservation_equals_usage':False}
  return {'content':json.dumps(content),'finish_reason':'stop','model':'fixture','usage':{'prompt_tokens':10,'completion_tokens':10},'latency_seconds':0.}

def execute_case(case,arm,client,invoke,repeat):
 start=time.monotonic();cpu=time.process_time();attempt_start=len(client.ledger.rows);record_start=len(client.records);error_type=None
 try:
  text=invoke(arm,case['messages'])
  if case['id']=='tool_chain':
   if digest(json.loads(text))!=digest(case['tool_request']):raise ValueError('Tool protocol rejected; do not request stage two')
   follow=case['messages']+[{'role':'assistant','content':json.dumps(case['tool_request'],sort_keys=True)},{'role':'user','content':'The observed inventory tool result is '+json.dumps(case['tool_result'],sort_keys=True)+'. Return JSON with sku and available=stock-reserved.'}]
   text=invoke(arm,follow)
  passed=grade(case,text)
 except Exception as exc:
  # Resource recovery failure is not an ordinary model-task failure.
  if isinstance(exc,RuntimeError) and str(exc).startswith(('CLEANUP_FAILED:','TEARDOWN_FAILED:')):raise
  passed=False;error_type=type(exc).__name__
 attempts=client.ledger.rows[attempt_start:];records=client.records[record_start:]
 return {'repeat':repeat,'case':case['id'],'arm':arm,'seconds':time.monotonic()-start,'coordinator_cpu_seconds':time.process_time()-cpu,'passed':passed,'request_hashes':[r['request_sha256'] for r in attempts],'calls':len(attempts),'usage':{k:sum(r['usage'][k] for r in records) for k in ('prompt_tokens','completion_tokens')},'usage_unknown_calls':sum('usage' not in r for r in attempts),'broker_seconds':sum(r.get('transport_timings',{}).get('broker_seconds',0) for r in records),'provider_seconds':sum(r.get('provider_seconds') or 0 for r in records),'case_error_type':error_type}

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--live',action='store_true');p.add_argument('--profile');p.add_argument('--aizex',action='store_true',help='Explicitly use Aizex gpt-6-luna and the user-supplied published nominal token prices');p.add_argument('--limit-usd',type=float,default=.05);p.add_argument('--output',type=Path,required=True);p.add_argument('--repeats',type=int,default=2);a=p.parse_args()
 if not 0<a.limit_usd<=.05:p.error('Spend limit must be positive and at most0.05 nominalUSD')
 if a.repeats!=2:p.error('This small preflight has exactly two repetitions (16 calls total)')
 a.output.mkdir(mode=0o700,parents=True,exist_ok=False)
 if a.live:
  from agent_runtime.model_broker import BrokerClient
  c=BrokerClient.from_env();info=c.profiles_info();row=next((x for x in info['profiles'] if x['id']==a.profile),None)
  if not row or row['provider']!=('aizex' if a.aizex else 'openai') or row['model']!=('gpt-6-luna' if a.aizex else MODEL) or not row['configured']:raise ValueError('Pin a configured official OpenAI gpt-4.1-mini profile')
  c=c.for_profile(a.profile,'gpt-6-luna' if a.aizex else MODEL)
 else:c=Fixture()
 from agent_runtime.runtime import run
 from agent_runtime.worker_session import WorkerSession
 ledger=Ledger(a.output/'ledger.json',limit=a.limit_usd,**({'input_price':.1,'output_price':.5,'price_source':'User-provided Aizex pricing screenshot 2026-11-17; nominal USD/million tokens; discounts ignored'} if a.aizex else {}));client=Measured(c,ledger);rows=[]
 with WorkerSession(a.output/'runs') as session:
  def call(arm,messages):
   if arm=='direct':return client.complete(messages,256)['content']
   states,events=run([{'id':'model','action':'llm','messages':messages,'max_tokens':256,'timeout':120}],quiet=True,output_dir=a.output/'runs',worker_session=session,model_client=client,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'),model_budget={'calls':1,'output_tokens':256})
   if states['model']!='succeeded':raise ValueError('Runtime execution failed; no paid retry')
   return next(e['result']['result'] for e in events if e['state']=='succeeded')
  try:
   for repeat in range(2):
    for case in cases():
     for arm in (('direct','runtime') if repeat==0 else ('runtime','direct')):
      row=execute_case(case,arm,client,call,repeat)
      rows.append(row);(a.output/'rows.json').write_text(json.dumps(rows,indent=2));print(json.dumps(row),flush=True)
  finally:session_stats=dict(session.stats)
 pairs=[(next(r for r in rows if r['repeat']==repeat and r['case']==case['id'] and r['arm']=='direct'),next(r for r in rows if r['repeat']==repeat and r['case']==case['id'] and r['arm']=='runtime')) for repeat in range(2) for case in cases()]
 matched=all(d['request_hashes']==r['request_hashes'] for d,r in pairs)
 prefix_matched=all(d['request_hashes'][:min(len(d['request_hashes']),len(r['request_hashes']))]==r['request_hashes'][:min(len(d['request_hashes']),len(r['request_hashes']))] for d,r in pairs)
 summary={'live':a.live,'supplier_requests':len(ledger.rows) if a.live else 0,'fixture_calls':len(ledger.rows) if not a.live else 0,'model':c.model,'provider':'aizex' if a.aizex else 'openai','paired_requests_identical':matched,'paired_attempted_prefixes_identical':prefix_matched,'repeats':2,'tasks_per_arm':6,'reserved_usd':ledger.reserved,'session':session_stats,'arms':{},'limits':['Six small tasks per arm, not an official capability score or significance result','Direct shares the same RAM broker, TLS path and parameters; no raw HTTP speed claim','Same explicit two-turn tool protocol and canonicalized messages in both arms','No prompts, model text or supplier credentials in public summary']}
 for arm in ('direct','runtime'):
  selected=[r for r in rows if r['arm']==arm];summary['arms'][arm]={'passed':sum(r['passed'] for r in selected),'seconds_sum':sum(r['seconds'] for r in selected),'seconds_median':statistics.median(r['seconds'] for r in selected),'calls':sum(r['calls'] for r in selected),'usage_unknown_calls':sum(r['usage_unknown_calls'] for r in selected),'usage':{k:sum(r['usage'][k] for r in selected) for k in ('prompt_tokens','completion_tokens')}}
 (a.output/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary),flush=True)
if __name__=='__main__':main()
