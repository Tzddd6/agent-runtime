import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from agent_runtime.failures import event_failure,model_code
from agent_runtime.chat_store import answer_for
from agent_runtime.execution_view import execution_view
from agent_runtime.model_gateway import ModelGateway,ModelError
from agent_runtime.runtime import run


class FailureScenarioTests(unittest.TestCase):
    def test_old_svg_502_is_explained_without_traceback_or_secrets(self):
        event={'task_id':'route','attempt':1,'time':2,'state':'failed','output':'private sk-DO-NOT-EXPOSE HTTP Error 502: Bad Gateway'}
        job={'created':1,'finished':2,'preset':'auto','status':'failed','events':[event]}
        text=answer_for(job);self.assertIn('HTTP 502',text);self.assertNotIn('sk-',text)
        view=execution_view(job);self.assertEqual(view['steps'][0]['failure']['code'],'MODEL_UPSTREAM_FAILED')
        self.assertNotIn('DO-NOT',str(view))

    def test_fault_categories_do_not_echo_provider_messages(self):
        for error,code in [('secret token: timeout','MODEL_TIMEOUT'),('secret key budget exceeded','MODEL_BUDGET_EXHAUSTED'),('secret value','MODEL_UPSTREAM_FAILED')]:
            self.assertEqual(model_code(error),code)
        self.assertEqual(event_failure({'state':'timeout'})['code'],'TASK_TIMEOUT')
        self.assertEqual(event_failure({'state':'failed','error':'OOM: raw'})['code'],'TASK_OOM')

    def test_model_failure_and_cut_off_svg_do_not_publish_success_or_retry(self):
        from agent_runtime import runtime
        for mode in ('upstream','length'):
            calls=[]
            class Client:
                model='offline';timeout=1
                def complete(self,messages,max_tokens):
                    calls.append(1)
                    if mode=='upstream':raise ModelError('arbitrary-private-credential')
                    return {'content':'<svg><g>incomplete','finish_reason':'length','usage':{'completion_tokens':256}}
            with tempfile.TemporaryDirectory() as tmp,patch.object(runtime.ModelGateway,'from_env',side_effect=lambda path:ModelGateway(Client(),path)):
                states,events=run([{'id':'route','action':'llm','messages':[{'role':'user','content':'generate SVG'}],'max_tokens':256},
                                   {'id':'independent','action':'echo','payload':'healthy'}],quiet=True,output_dir=tmp)
                self.assertEqual(states,{'route':'failed','independent':'succeeded'})
                self.assertEqual(len(calls),1)
                event=next(e for e in events if e['task_id']=='route' and e['state']=='failed')
                self.assertEqual(event['result']['failure']['code'],'MODEL_UPSTREAM_FAILED' if mode=='upstream' else 'MODEL_OUTPUT_INCOMPLETE')
                self.assertNotIn('arbitrary-private',str(event))


if __name__=='__main__':unittest.main()
