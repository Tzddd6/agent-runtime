import json,tempfile,unittest
from pathlib import Path
from tools.benchmark_interactive import direct,grade

class InteractiveHarnessTests(unittest.TestCase):
    def test_direct_tools_really_write_and_grader_rejects_a_wrong_artifact(self):
        class Client:timeout=2;model='unused'
        with tempfile.TemporaryDirectory() as tmp:
            sample=Path(tmp);workspace=sample/'workspace';workspace.mkdir()
            states,events=direct([{'id':'write','action':'plugin','plugin':'file.write','arguments':{'path':'receipt.json','content':'{"total_cents":875}'}}],workspace,sample,Client())
            self.assertEqual(states,{'write':'succeeded'})
            case={'group':'upload','fixture':'csv','expected':{'total_cents':875}}
            self.assertTrue(grade(case,'',[],workspace,sample,'x')['passed'])
            (workspace/'receipt.json').write_text('{"total_cents":874}')
            self.assertFalse(grade(case,'',[],workspace,sample,'x')['passed'])
    def test_computer_claim_alone_cannot_pass_quality(self):
        case={'group':'computer','expected':['PINE-442：预算95元，负责人Cedar。']}
        values=[{'plugin':'computer.run','ok':True,'answer':'done','observation':{'text':'not observed'},'steps':[{'action':'click','succeeded':True}]}]
        self.assertFalse(grade(case,'done',values,None,None,'x')['passed'])
