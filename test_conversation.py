import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent_runtime.conversation import review_tasks,route_review
from agent_runtime.model_gateway import ModelGateway
from agent_runtime.runtime import run


class ConversationTests(unittest.TestCase):
    def test_malformed_critique_is_rejected(self):
        gate=review_tasks('task')[-1]
        for text in ('not-json','[]','{"decision":"approved","feedback":3}',
                     '{"decision":"execute_command","feedback":"x"}'):
            with self.assertRaises(ValueError):
                route_review({**gate,'_inputs':{'writer-1':'answer','critic-1':text}})

    def test_real_worker_feedback_creates_bounded_second_round(self):
        class Client:
            model,timeout='offline-review-fixture',5
            def __init__(self):self.calls=0
            def complete(self,messages,max_tokens):
                self.calls+=1
                if self.calls%2==0:
                    content=json.dumps({'decision':'revise','feedback':'add a test'})
                else:content='draft '+str(self.calls)
                return {'content':content,'model':self.model,'usage':{}}
        with tempfile.TemporaryDirectory() as directory:
            client=Client()
            gateway=ModelGateway(client,Path(directory)/'calls.jsonl')
            with patch('agent_runtime.runtime.ModelGateway.from_env',return_value=gateway):
                states,events=run(review_tasks('task'),output_dir=directory,quiet=True)
            self.assertTrue(all(s=='succeeded' for s in states.values()),events)
            self.assertEqual(client.calls,4)
            self.assertEqual(len(states),6)
            outcome=next(e['result']['result'] for e in events if e['task_id']=='gate-2' and e['state']=='succeeded')
            self.assertEqual(outcome['termination'],'max_rounds')
            self.assertFalse(outcome['accepted'])
            self.assertEqual(outcome['answer'],'draft 3')

    def test_approved_ends_without_extra_model_calls(self):
        gate=review_tasks('task')[-1]
        result,children=route_review({**gate,'_inputs':{
            'writer-1':'answer','critic-1':json.dumps({'decision':'approved','feedback':'ok'})}})
        self.assertEqual(children,[])
        self.assertTrue(result['accepted'])


if __name__=='__main__':unittest.main()
