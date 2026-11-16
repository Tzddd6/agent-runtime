import io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from tools.benchmark_execution_quality import child,ARMS
from agent_runtime.compact_patch import compile_patch
from agent_runtime.engineering import manifest
from test_v18_execution import PRICING_EDITS


class QualityHarnessTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='posix','real worker/sandbox harness')
    def test_all_four_arms_execute_and_deliver_actual_verified_source_without_supplier_calls(self):
        source=compile_patch(json.dumps(PRICING_EDITS),'pricing')
        def complete(client,messages,max_tokens):
            value=PRICING_EDITS if '"edits"' in messages[0]['content'] else {'source':source}
            return {'content':json.dumps(value),'usage':{'prompt_tokens':1,'completion_tokens':1},'finish_reason':'stop'}
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'AGENTRT_PROXY_TOKEN':'offline-harness-token'}),patch('agent_runtime.model_broker.BrokerClient.complete',complete):
            root=Path(folder);case={'project':'pricing','files':{},'prompt':'修复这个Python项目，交付已测源码。\n'+json.dumps(manifest('pricing'),ensure_ascii=False)}
            (root/'dataset.json').write_text(json.dumps({'cases':[case]}))
            for arm in ARMS:
                with patch('sys.stdin',io.StringIO('G\n')):child(str(root),0,arm,None)
                result=json.loads((root/f'00-{arm}-result.json').read_text())
                self.assertEqual(result['status'],'completed',arm);self.assertTrue(result['accepted'],arm)
                self.assertEqual(result['source'],source if arm!='direct_full' else source)
                self.assertEqual(len(result['calls']),1)


if __name__=='__main__':unittest.main()
