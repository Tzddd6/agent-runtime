"""Offline paired transport ablation: fixed 300 ms provider, identical requests."""
import argparse,json,statistics,threading,time
from pathlib import Path
from unittest.mock import patch
from agent_runtime.model_broker import BrokerClient,make_server
from agent_runtime.model_profiles import MultiModelBroker
TOKEN='synthetic-wait-benchmark-control'
class Fixture:
 def complete(self,messages,max_tokens):time.sleep(.3);return {'content':'fixed result','finish_reason':'stop','usage':{'prompt_tokens':10,'completion_tokens':10}}
def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 if a.output.exists():raise ValueError('Use new output')
 b=MultiModelBroker(TOKEN,profile_factory=lambda *args:Fixture());b.save_profile({'id':'fixture','label':'Fixture','provider':'openai','model':'fixture','key':'synthetic-not-a-real-supplier-key'})
 server=make_server(b,0);t=threading.Thread(target=server.serve_forever,daemon=True);t.start();url='http://127.0.0.1:'+str(server.server_port);rows=[]
 try:
  for repeat in range(5):
   for arm in (('poll','wait') if repeat%2==0 else ('wait','poll')):
    c=BrokerClient(TOKEN,url,local_test=True);c.profile_id='fixture';c.profile_revision=1
    start=time.monotonic();cpu=time.process_time()
    with patch('agent_runtime.model_broker.FEDERATED_BROKER_URL',url if arm=='wait' else 'http://127.0.0.1:1'):
     result=c.complete([{'role':'user','content':'fixed'}],10)
    assert result['content']=='fixed result'
    rows.append({'repeat':repeat,'arm':arm,'seconds':time.monotonic()-start,'process_cpu_seconds':time.process_time()-cpu,'status_reads':result['transport_timings']['status_reads'],'detection_seconds':result['transport_timings']['completion_detection_seconds']})
 finally:b.close();server.shutdown();server.server_close();t.join()
 summary={'supplier_requests':0,'fixture_calls':10,'fixed_provider_seconds':.3,'all_results_identical':True,'rows':rows,'arms':{},'limits':['Synthetic 300ms fixture; not real model latency or quality','CPU includes client and local server threads in the same process; no deployment-wide RSS claim']}
 for arm in ('poll','wait'):
  selected=[r for r in rows if r['arm']==arm];summary['arms'][arm]={k:statistics.median(r[k] for r in selected) for k in ('seconds','process_cpu_seconds','status_reads','detection_seconds')}
 a.output.write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary))
if __name__=='__main__':main()
