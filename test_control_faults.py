"""Control-plane fault regression tests; no vendor calls or deployment."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_runtime.web import Controller, make_server


class ControlFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.controller = Controller(self.temp.name)

    def wait_finished(self, key):
        deadline = time.monotonic()+4
        while time.monotonic() < deadline:
            with self.controller.lock:
                job = dict(self.controller.jobs[key])
            if 'finished' in job:
                return job
            time.sleep(.005)
        self.fail('background controller did not finish')

    def server(self):
        server = make_server(self.controller, port=0, proxy_token='offline-proxy')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}'

    def request(self, base, path, body=None):
        headers = {'X-AgentRT-Proxy-Token':'offline-proxy',
                   'X-AgentRT-CSRF':self.controller.csrf}
        data = None if body is None else json.dumps(body).encode()
        return urlopen(Request(base+path, data=data, headers=headers), timeout=4)

    def test_unconfirmed_failure_is_quarantined_logged_and_blocks_new_work(self):
        callbacks = []
        marker = 'private-fault-marker-not-for-browser'
        sample = {'time':time.time(), 'tasks':[{'id':'a', 'state':'running',
                  'metrics':{'cpu_cores':.1,'memory_bytes':1024}}],
                  'reserved':{'cpu_milli':1000,'memory_bytes':1024,'pids':1},'slots_used':1}
        def fail(*args, **kwargs):
            callbacks.append(kwargs['observer'])
            kwargs['observer'](sample)
            raise RuntimeError('CLEANUP_FAILED: '+marker)
        with self.assertLogs('agent_runtime.web', level='ERROR') as logs:
            with patch('agent_runtime.web.run', side_effect=fail):
                key = self.controller.submit({'preset':'dynamic'})
                job = self.wait_finished(key)
        self.assertEqual(job['status'], 'recovery_required')
        self.assertTrue(job['monitor']['stale'])
        self.assertTrue(self.controller.snapshot()['recovery_required'])
        with self.assertRaisesRegex(ValueError, '资源回收状态未确认'):
            self.controller.submit({'preset':'dynamic'})
        # A late asynchronous observation cannot resurrect live metrics.
        callbacks[0](sample)
        self.assertTrue(self.controller.detail(key)['monitor']['stale'])
        self.assertNotIn(marker, json.dumps(self.controller.detail(key)))
        self.assertTrue(any(key in line and marker in line for line in logs.output))

    def test_failure_before_first_snapshot_is_also_quarantined(self):
        with self.assertLogs('agent_runtime.web', level='ERROR'):
            with patch('agent_runtime.web.run', side_effect=OSError('offline startup failure')):
                key = self.controller.submit({'preset':'dynamic'})
                job = self.wait_finished(key)
        self.assertEqual(job['status'],'recovery_required')
        self.assertTrue(job['monitor']['stale'])

    def test_normal_task_failure_does_not_quarantine_service(self):
        with patch('agent_runtime.web.run', return_value=({'a':'failed'},[])):
            first = self.controller.submit({'preset':'dynamic'})
            self.assertEqual(self.wait_finished(first)['status'],'failed')
            self.assertFalse(self.controller.snapshot()['recovery_required'])
            second = self.controller.submit({'preset':'dynamic'})
            self.wait_finished(second)

    def test_quarantine_blocks_key_replace_and_clear_without_exposing_key(self):
        base = self.server()
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY':'offline-original-key'}):
            self.controller.recovery_required = True
            for key in ('', 'offline-replacement-key'):
                with self.assertRaises(HTTPError) as error:
                    self.request(base,'/api/model-config',{'key':key})
                self.assertEqual(error.exception.code,400)
                payload=error.exception.read().decode()
                self.assertIn('资源回收状态未确认',payload)
                self.assertNotIn('offline-original-key',payload)
                self.assertEqual(os.environ['DEEPSEEK_API_KEY'],'offline-original-key')
            status=json.load(self.request(base,'/api/status'))
            self.assertTrue(status['recovery_required'])
            self.assertNotIn('offline-original-key',json.dumps(status))

    def test_key_validation_and_admission_share_config_lock(self):
        class ObservedLock:
            def __init__(self):
                self.inner=threading.Lock()
                self.entered=threading.Event()
            def acquire(self):return self.inner.acquire()
            def release(self):return self.inner.release()
            def __enter__(self):
                self.entered.set()
                self.inner.acquire()
                return self
            def __exit__(self,*args):self.inner.release()
        lock=ObservedLock()
        self.controller.lock=lock
        results,errors=[],[]
        def submit():
            try:results.append(self.controller.submit({'preset':'llm','prompt':'offline'}))
            except ValueError as error:errors.append(str(error))
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY':'offline-original-key'}):
            with patch('agent_runtime.web.run',return_value=({'a':'succeeded'},[])):
                lock.acquire()
                thread=threading.Thread(target=submit)
                try:
                    thread.start()
                    self.assertTrue(lock.entered.wait(3))
                    # Equivalent to model-config clearing while it owns this lock.
                    os.environ.pop('DEEPSEEK_API_KEY')
                finally:lock.release()
                thread.join(3)
                self.assertFalse(thread.is_alive())
        self.assertFalse(results)
        self.assertEqual(len(errors),1)
        self.assertIn('尚未配置',errors[0])
        self.assertFalse(self.controller.jobs)

    def test_busy_workflow_blocks_key_update_and_cancel_remains_effective(self):
        base=self.server()
        started=threading.Event()
        def wait_for_cancel(*args,**kwargs):
            started.set()
            self.assertTrue(kwargs['cancel_event'].wait(3))
            return {'a':'cancelled'},[]
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY':'offline-original-key'}):
            with patch('agent_runtime.web.run',side_effect=wait_for_cancel):
                key=self.controller.submit({'preset':'dynamic'})
                self.assertTrue(started.wait(3))
                try:
                    with self.assertRaises(HTTPError) as error:
                        self.request(base,'/api/model-config',{'key':'offline-replacement-key'})
                    self.assertEqual(error.exception.code,400)
                    self.assertEqual(os.environ['DEEPSEEK_API_KEY'],'offline-original-key')
                finally:
                    self.request(base,'/api/jobs/'+key+'/cancel',{}).close()
                self.assertEqual(self.wait_finished(key)['status'],'cancelled')
                self.assertFalse(self.controller.snapshot()['recovery_required'])

    def test_snapshot_watermark_excludes_newer_journal_events(self):
        root=Path(self.temp.name)/'job'
        workflow=root/'workflow'
        workflow.mkdir(parents=True)
        events=[{'sequence':1,'task_id':'a','state':'running'},
                {'sequence':2,'task_id':'a','state':'succeeded'}]
        (workflow/'events.jsonl').write_text('\n'.join(map(json.dumps,events))+'\n')
        self.controller.jobs['job']={'id':'job','status':'running','directory':root,
            'cancel':threading.Event(),'history':[],
            'monitor':{'event_sequence':1,'tasks':[{'id':'a','state':'running'}]}}
        detail=self.controller.detail('job')
        self.assertEqual(detail['states'],{'a':'running'})
        self.assertEqual([event['sequence'] for event in detail['events']],[1])
        self.controller.jobs['job']['monitor']={'event_sequence':2,'tasks':[{'id':'a','state':'succeeded'}]}
        self.assertEqual(self.controller.detail('job')['states'],{'a':'succeeded'})

    @unittest.skipUnless(shutil.which('node'),'Node.js is needed to execute browser logic offline')
    def test_browser_disconnect_invalidates_live_readings_and_controls(self):
        source=Path(__file__).parent/'agent_runtime/static/console.js'
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element{constructor(tag){this.tag=tag;this.children=[];this.style={};this.attributes={};this._text='';this.disabled=false}set textContent(v){this._text=String(v);this.children=[]}get textContent(){return this._text+this.children.map(x=>x.textContent||'').join('')}append(...items){this.children.push(...items)}replaceChildren(...items){this.children=items;this._text=''}setAttribute(k,v){this.attributes[k]=v}}
const nodes=new Map();const document={getElementById(id){if(id==='form')return null;if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id)},createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag),createTextNode:text=>({textContent:text})};
const context={document,Date,console,AbortController,setTimeout,clearTimeout,setInterval(){},fetch:async()=>{throw Error('offline connection')}};vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const job={id:'job',status:'running',created:10,events:[{task_id:'a',attempt:1,state:'running',time:10,pid:100}],states:{a:'running'},budget:{cpu_milli:2000,memory_bytes:1073741824,pids:128},monitor:{time:11,enforcement:'cgroup_v2',tasks:[{id:'a',agent:'a',state:'running',attempt:1,pid:100,resources:{cpu_milli:1000,memory_bytes:268435456,pids:32},metrics:{cpu_cores:.7,memory_bytes:20000000,pids:1}}],reserved:{cpu_milli:1000,memory_bytes:268435456,pids:32},budget:{cpu_milli:2000,memory_bytes:1073741824,pids:128},slots_used:1,slots_total:2},history:[{time:11,cpu_cores:.7,memory_bytes:20000000,reserved:{cpu_milli:1000,memory_bytes:268435456}}]};
context.renderDetail(job);context.renderMachine({host:{cpu_percent:87,memory_used_bytes:100000000},service:{cpu_cores:.7,memory_bytes:20000000}});vm.runInContext("selected='job'",context);
(async()=>{await context.refresh();assert(nodes.get('connection').textContent.includes('中断'));assert.strictEqual(nodes.get('hostCpu').textContent,'—');assert.strictEqual(nodes.get('serviceMemory').textContent,'—');assert(nodes.get('cpuReadout').textContent.startsWith('—'));assert(nodes.get('sampleNote').textContent.includes('采样已失效'));for(const id of ['submit','cancel','saveKey'])assert(nodes.get(id).disabled);for(const card of nodes.get('allocations').children)assert.strictEqual(card.children[2].children.length,0);assert(nodes.get('cpuRange').textContent.startsWith('最后记录'));
context.fetch=async url=>({ok:true,json:async()=>url==='api/status'?{csrf:'offline-csrf',jobs:[{id:'job',preset:'dynamic',status:'running',created:10}],enforcement:'cgroup_v2',model_configured:false,machine:{}}:job});await context.refresh();assert(!nodes.get('connection').textContent.includes('中断'));assert.strictEqual(nodes.get('cancel').disabled,false);
context.fetch=async url=>({ok:true,json:async()=>url==='api/status'?{csrf:'offline-csrf',recovery_required:true,recovery_message:'管理员核验后恢复',jobs:[{id:'job',preset:'dynamic',status:'recovery_required',created:10}],enforcement:'cgroup_v2',machine:{}}:{...job,status:'recovery_required',monitor:{...job.monitor,stale:true}}});await context.refresh();assert(nodes.get('notice').textContent.includes('管理员'));for(const id of ['submit','cancel','saveKey'])assert(nodes.get(id).disabled);console.log('offline UI guards passed')})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result=subprocess.run(['node','-',str(source)],input=script,text=True,
                              capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('offline UI guards passed',result.stdout)


if __name__=='__main__':
    unittest.main()
