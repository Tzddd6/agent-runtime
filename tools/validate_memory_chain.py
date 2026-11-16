"""End-to-end component score and deliberate real-chain fault sensitivity.

Synthetic provider responses; no supplier credentials, native AIOS leaderboard
claim, or performance attribution. Run on Linux, optionally delegated cgroup v2.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    target=ROOT/'reports/memory-chain-final';target.mkdir(parents=True,exist_ok=False)
    env={k:v for k,v in os.environ.items() if k not in ('DEEPSEEK_API_KEY','AGENTRT_MODEL_BROKER_URL','AGENTRT_MODEL_BROKER_TOKEN','AGENTRT_PROXY_TOKEN')}
    regular=subprocess.run([sys.executable,'-m','unittest','-v','test_memory_runtime_chain'],cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    (target/'normal.txt').write_text(regular.stdout)
    cases={
        'tenant_partition':("from agent_runtime.tenants import TenantRouter; TenantRouter.resolve=lambda self,user:self.base",'test_http_memory_injected_into_actual_worker_gateway_and_cross_user_denied'),
        'private_agent':("from agent_runtime.memory_access import MemoryContext; orig=MemoryContext.allowed; MemoryContext.allowed=staticmethod(lambda m,u,a:orig(m,u,m.get('owner_agent') if isinstance(m,dict) else a))",'test_team_private_excluded_shared_visible_for_actual_four_agents'),
        'budget':("from agent_runtime.memory_repository import MemoryRepository; orig=MemoryRepository.inject; MemoryRepository.inject=lambda self,a,m,b=8192:orig(self,a,m,32768)",'test_http_budget_bound_at_actual_provider_boundary'),
        'fail_open':("from agent_runtime.memory_repository import MemoryRepository\norig=MemoryRepository.inject\ndef unsafe(self,a,m,b=8192):\n try:return orig(self,a,m,b)\n except Exception:return list(m),{}\nMemoryRepository.inject=unsafe",'test_database_failure_blocks_provider_and_manual_restore_recovers'),
    }
    mutants={}
    for name,(patch,test) in cases.items():
        code=patch+"\nimport unittest\nsuite=unittest.defaultTestLoader.loadTestsFromName('test_memory_runtime_chain.RuntimeMemoryTests."+test+"')\nr=unittest.TextTestRunner(verbosity=2).run(suite)\nraise SystemExit(0 if r.wasSuccessful() else 1)"
        result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=45)
        (target/(name+'.txt')).write_text(result.stdout)
        # A traceback/import error is not evidence that an assertion caught a fault.
        mutants[name]={'caught':result.returncode!=0 and 'FAIL:' in result.stdout and 'ERROR:' not in result.stdout}
    import re
    count=re.search(r'Ran (\d+) tests?',regular.stdout)
    summary={'normal_tests':int(count[1]) if count else None,'passed':regular.returncode==0,
             'mutations':mutants,'mutations_caught':sum(v['caught'] for v in mutants.values()),
             'supplier_calls':0,'fixture_provider':True,
             'real_chain':'HTTP -> per-user Controller -> Runtime -> Worker -> ModelGateway -> fixed provider',
             'cgroup_v2':bool(env.get('AGENTRT_TEST_CGROUP')),'ranking':None,
             'ranking_note':'Internal contract/chain checks, not an official leaderboard submission'}
    (target/'summary.json').write_text(json.dumps(summary,indent=2))
    print(regular.stdout);print(json.dumps(summary,indent=2))
    raise SystemExit(0 if summary['passed'] and summary['mutations_caught']==4 else 1)


if __name__=='__main__':main()
