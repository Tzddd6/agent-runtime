import tempfile,unittest
from pathlib import Path
from agent_runtime.context import ContextStore
from agent_runtime.context_control import ContextGovernor


class GoalHistoryTests(unittest.TestCase):
    def test_large_continuation_preserves_all_prior_user_constraints_verbatim(self):
        with tempfile.TemporaryDirectory() as folder:
            store=ContextStore(Path(folder));goal='原始图形约束：'+'甲'*15000+'末尾必须保留五个苹果'
            changes='追加约束：只修改右侧，不改原数量。'
            value={'user_request':'继续完成，保留上述约束。','conversation':[{'role':'user','content':goal},
                   {'role':'assistant','content':'旧草稿'*12000},{'role':'user','content':changes}]}
            reference=store.put(value,['agent']);view=ContextGovernor(store).view('agent',[reference],{})
            context=view['body']['context'][0]
            self.assertEqual(context['user_request'],value['user_request'])
            self.assertEqual(context['prior_user_requests'],[goal,changes])
            self.assertTrue(context['partial']);self.assertEqual(view['body']['context_control']['history_requests_preserved'],2)
            self.assertEqual(store.get(reference,'agent'),value)

    def test_preserved_history_above_hard_limit_is_rejected_instead_of_silently_cut(self):
        with tempfile.TemporaryDirectory() as folder:
            store=ContextStore(Path(folder));value={'user_request':'继续','conversation':[{'role':'user','content':'x'*12000}]}
            reference=store.put(value,['agent'])
            with self.assertRaises(ValueError):ContextGovernor(store,soft_bytes=4096,hard_bytes=8192).view('agent',[reference],{})
            self.assertEqual(store.get(reference,'agent'),value)
