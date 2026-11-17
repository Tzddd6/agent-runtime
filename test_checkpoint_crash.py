"""Actual coordinator SIGKILL, not a simulated successful recovery receipt."""
import json,os,signal,subprocess,sys,tempfile,time,unittest,uuid
from pathlib import Path
from agent_runtime.runtime import run
from agent_runtime.checkpoint import RecoveryBlocked,read_checkpoint


@unittest.skipUnless(sys.platform=='linux' and os.environ.get('AGENTRT_TEST_CGROUP'),'needs delegated cgroup v2')
class CheckpointCrashTests(unittest.TestCase):
    def test_killed_coordinator_requires_cleanup_and_preserves_completed_write(self):
        parent=Path(os.environ['AGENTRT_TEST_CGROUP']);groups=[];child=None
        def group():
            p=parent/('recovery-fixture-'+uuid.uuid4().hex);p.mkdir()
            (p/'cgroup.subtree_control').write_text('+cpu +memory +pids');groups.append(p);return p
        try:
            old,new=group(),group()
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);workspace=root/'work';workspace.mkdir()
                tasks=[{'id':'write','action':'command','argv':['/bin/sh','-c',f'echo retained >> {workspace}/count']},
                       {'id':'pause','action':'sleep','seconds':3,'timeout':8,'depends_on':['write']}]
                # Explicit spawn leaves a real orphan on coordinator SIGKILL.
                # Forkserver parent-death cleanup can legitimately beat the check.
                options=dict(output_dir=str(root/'runs'),workspace=str(workspace),checkpoint_dir=str(root/'checkpoint'),recovery_owner='alice',quiet=True,worker_backend='spawn')
                source='from agent_runtime.runtime import run;run('+repr(tasks)+',max_workers=1,cgroup_root='+repr(str(old))+',**'+repr(options)+')'
                child=subprocess.Popen([sys.executable,'-c',source],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    env={'PATH':os.defpath,'LANG':'C.UTF-8','PYTHONPATH':str(Path(__file__).resolve().parent)})
                end=time.monotonic()+5
                while time.monotonic()<end:
                    try:data=read_checkpoint(root/'checkpoint','alice')
                    except (OSError,ValueError):data={}
                    if data.get('states',{}).get('write')=='succeeded' and data.get('states',{}).get('pause')=='running':break
                    time.sleep(.01)
                else:self.fail('Child did not reach real second Attempt')
                # Wait for actual Worker placement, not only the prelaunch intent.
                end=time.monotonic()+2
                while 'populated 1' not in (old/'cgroup.events').read_text() and time.monotonic()<end:time.sleep(.005)
                os.kill(child.pid,signal.SIGKILL);child.wait(timeout=3)
                with self.assertRaises(RecoveryBlocked):run(tasks,max_workers=1,cgroup_root=str(new),resume=True,**options)
                (old/'cgroup.kill').write_text('1')
                end=time.monotonic()+3
                while 'populated 1' in (old/'cgroup.events').read_text() and time.monotonic()<end:time.sleep(.01)
                states,events=run(tasks,max_workers=1,cgroup_root=str(new),resume=True,**options)
                self.assertEqual(states,{'write':'succeeded','pause':'succeeded'})
                self.assertEqual((workspace/'count').read_text(),'retained\n')
                self.assertEqual(sum(e['task_id']=='write' and e['state']=='running' for e in events),1)
                self.assertEqual(read_checkpoint(root/'checkpoint','alice')['attempts']['pause'],2)
        finally:
            if child is not None and child.poll() is None:child.kill();child.wait(timeout=3)
            for scope in groups:
                (scope/'cgroup.kill').write_text('1')
                for p in sorted(scope.rglob('*'),key=lambda p:len(p.parts),reverse=True):
                    if p.is_dir():
                        end=time.monotonic()+2
                        while 'populated 1' in (p/'cgroup.events').read_text() and time.monotonic()<end:time.sleep(.01)
                        p.rmdir()
                scope.rmdir()
