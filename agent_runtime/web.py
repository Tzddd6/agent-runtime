"""受保护的轻量 Runtime 控制台，仅接受固定演示或模型对话，不开放任意命令。"""
import argparse
import hmac
import json
import logging
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from .runtime import run
from .telemetry import MachineSampler
from .model_gateway import ModelError
from .job_store import JobStore
from .agents import RuntimeControl


DEMO = Path(__file__).resolve().parents[1] / 'examples/dynamic.json'
LOGGER = logging.getLogger(__name__)
RECOVERY_MESSAGE = '执行资源回收状态未确认，已暂停新任务和模型配置；请管理员检查日志与残留进程/cgroup，确认回收并归档隔离索引后重启本项目服务'


class Controller:
    def __init__(self, root, cgroup_root=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cgroup_root = cgroup_root
        self.csrf = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.jobs = {}
        self.store=JobStore(self.root)
        self.machine = MachineSampler(cgroup_root)
        self.recovery_required = False
        self.storage_warning=False
        for job in self.store.load():
            self.jobs[job['id']]=job
            if job['status']=='recovery_required':self.recovery_required=True
        self.model_broker = None
        if os.environ.get('AGENTRT_MODEL_BROKER_URL'):
            from .model_broker import BrokerClient
            self.model_broker=BrokerClient.from_env()

    def model_info(self):
        if self.model_broker is not None:
            try:return {**self.model_broker.status(),'available':True}
            except ModelError:return {'configured':False,'model':self.model_broker.model,'available':False}
        return {'configured':bool(os.environ.get('DEEPSEEK_API_KEY')),
                'model':os.environ.get('DEEPSEEK_MODEL','deepseek-flash'),'available':True}

    def resource_budget(self,service):
        cpu=2000
        if service['cpu_limit_cores'] is not None:
            cpu=min(cpu,max(1,int(service['cpu_limit_cores']*1000)))
        memory=1024**3
        if service['memory_limit_bytes'] is not None:
            memory=min(memory,max(1,service['memory_limit_bytes']-128*1024**2))
        pids=128
        if service.get('pids_limit') is not None:pids=min(pids,max(1,service['pids_limit']-16))
        return {'cpu_milli':cpu,'memory_bytes':memory,'pids':pids}

    def submit(self, value):
        preset = value.get('preset')
        workers = value.get('workers', 2)
        policy = value.get('policy', 'resource_fit')
        if type(workers) is not int or not 1 <= workers <= 4 or policy not in ('fifo', 'resource_fit', 'aging_fit'):
            raise ValueError('并发数需为 1—4，调度策略不合法')
        if preset == 'dynamic':
            tasks = json.loads(DEMO.read_text(encoding='utf-8'))
        elif preset == 'resources':
            from .scenarios import resource_tasks
            tasks = resource_tasks()
        elif preset == 'agents':
            from .scenarios import agent_tasks
            tasks,agent_specs=agent_tasks()
        elif preset == 'ipc':
            tasks = json.loads((DEMO.parent/'ipc.json').read_text(encoding='utf-8'))
        elif preset == 'document':
            from .scenarios import tasks_for
            text = value.get('prompt', '') or 'Agent Runtime shares context. Agent Runtime schedules tasks.'
            if not isinstance(text, str) or len(text) > 4000:
                raise ValueError('文档长度不得超过 4000 字')
            tasks = tasks_for(text)
        elif preset == 'engineering':
            from .project_workflow import project_tasks
            project=value.get('project','pricing')
            if not isinstance(project,str):raise ValueError('项目类型无效')
            tasks=project_tasks(project)
        elif preset in ('llm','team','review'):
            prompt = value.get('prompt', '')
            if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
                raise ValueError('请输入 1—4000 字的提示词')
            tasks = [{'id':'assistant', 'agent':'assistant', 'action':'llm', 'timeout':75,
                      'max_tokens':512, 'messages':[{'role':'user','content':prompt}]}]
            if preset == 'team':
                from .scenarios import team_tasks
                tasks = team_tasks(prompt)
            elif preset == 'review':
                from .conversation import review_tasks
                tasks = review_tasks(prompt)
        else:
            raise ValueError('只能运行动态演示或模型对话，不接受任意任务/命令')
        with self.lock:
            if self.recovery_required:
                raise ValueError(RECOVERY_MESSAGE)
            if any(j['status'] in ('running','cancelling') for j in self.jobs.values()):
                raise ValueError('已有工作流运行中，请等待结束或停止后再提交')
            # The same lock guards model-config updates: no clear/replace can race
            # between this check and registering the active workflow.
            if preset in ('llm','team','review','engineering') and not self.model_info()['configured']:
                raise ValueError('模型服务尚未配置 API 密钥或暂不可用')
            key = uuid.uuid4().hex
            service = self.machine.sample()['service']
            # Worker count changes concurrency only; keep the same resource envelope.
            budget = self.resource_budget(service)
            job = {'id':key,'preset':preset,'status':'running','created':time.time(),
                   'workers':workers,'policy':policy,'budget':budget,'monitor':None,'history':[],
                   'cancel':threading.Event(), 'directory':self.root/key}
            job['_control']=RuntimeControl()
            if preset=='engineering':job['project']=project
            self.store.save(job) # Persist the sole submission intent before starting any Worker.
            self.jobs[key] = job
            # 内存索引保留最近 20 次，磁盘结果保留供团队管理。
            for old in list(self.jobs)[:-20]:
                if self.jobs[old]['status'] not in ('running','cancelling'):
                    del self.jobs[old]

        def observe(sample):
            running = [t for t in sample['tasks'] if t['state']=='running']
            measured = [t['metrics'] for t in running]
            def total(name):
                values = [m.get(name) for m in measured]
                return sum(values) if all(v is not None for v in values) else None
            frame = {'time':sample['time'],'reserved':sample['reserved'],
                     'cpu_cores':total('cpu_cores'),'memory_bytes':total('memory_bytes'),
                     'slots_used':sample['slots_used']}
            with self.lock:
                # An asynchronous observer may deliver an older sample after run
                # raised. It must never turn a quarantined job back into live data.
                job['monitor']={**sample,'stale':True} if job['status']=='recovery_required' else sample
                history=job['history']
                if (not history or frame['time']-history[-1]['time']>=.35
                        or frame['reserved']!=history[-1]['reserved']):
                    history.append(frame)
                    del history[:-150]

        def execute():
            try:
                states, completed_events = run(tasks, workers, 75, output_dir=job['directory'], policy=policy,
                                cgroup_root=self.cgroup_root, cancel_event=job['cancel'], quiet=True,
                                budget=budget,observer=observe,control=job['_control'],
                                agents=agent_specs if preset=='agents' else None)
                with self.lock:
                    job['final_states']=states
                    for summary_path in job['directory'].glob('*/summary.json'):
                        summary=json.loads(summary_path.read_text(encoding='utf-8'))
                        job['agent_summary']=summary.get('agents',[])
                        job['agent_control_summary']=summary.get('agent_controls',[])
                    semantic=True
                    if preset=='engineering':
                        from .project_workflow import project_view
                        summary=project_view(project,completed_events,states,budget)
                        semantic=isinstance(summary['verdict'],dict) and summary['verdict'].get('accepted') is True
                    job['status'] = ('cancelled' if any(s=='cancelled' for s in states.values())
                                     else 'succeeded' if all(s=='succeeded' for s in states.values()) and semantic else 'failed')
            except Exception:
                LOGGER.exception('Workflow %s exited without confirmed resource release; recovery required', key)
                with self.lock:
                    self.recovery_required=True
                    job['status']='recovery_required'
                    job['error']=RECOVERY_MESSAGE
                    job['monitor']={**(job['monitor'] or {}),'stale':True}
            finally:
                with self.lock:
                    job['finished']=time.time()
                    job['_control'].close()
                    try:self.store.save(job)
                    except (OSError,ValueError):
                        self.storage_warning=True
                        LOGGER.exception('Console index write failed for job %s; execution result retained in memory',key)

        threading.Thread(target=execute,daemon=True).start()
        return key

    def snapshot(self):
        with self.lock:
            jobs = [{k:v for k,v in j.items() if k not in ('cancel','directory','monitor','history','final_states','_control','agent_summary','agent_control_summary')} for j in self.jobs.values()]
            recovery_required = self.recovery_required
        info=self.model_info()
        machine=self.machine.sample()
        return {'jobs':list(reversed(jobs)), 'csrf':self.csrf,
                'model_configured':info['configured'],'model':info['model'],
                'model_available':info['available'],'fixed_budget':True,
                'storage_warning':self.storage_warning,'history_persistent':True,
                'enforcement':'cgroup_v2' if self.cgroup_root else 'admission_only',
                'recovery_required':recovery_required,
                'recovery_message':RECOVERY_MESSAGE if recovery_required else '',
                'machine':machine,'budget':self.resource_budget(machine['service'])}

    def detail(self, key):
        with self.lock:
            job = self.jobs.get(key)
            if not job:
                raise KeyError(key)
            result = {k:v for k,v in job.items() if k not in ('cancel','directory','history','_control')}
            result['history']=list(job['history'])
            directory = job['directory']
        events=[]
        for path in directory.glob('*/events.jsonl'):
            # 页面只读取最后 256 KiB；不把大文件完整装入内存。
            with path.open('rb') as f:
                f.seek(0,2)
                start=max(0,f.tell()-262144)
                f.seek(start)
                raw=f.read().decode('utf-8',errors='replace').splitlines()
                if start:raw=raw[1:]
            for line in raw:
                try:events.append(json.loads(line))
                except ValueError:pass
        monitor=result.get('monitor')
        watermark=monitor.get('event_sequence') if monitor and result['status'] in ('running','cancelling') else None
        if type(watermark) is int:
            # The observer may lag the journal: only return events belonging to
            # this snapshot, never combine a new terminal event with old usage.
            events=[event for event in events if type(event.get('sequence')) is int
                    and event['sequence']<=watermark]
        result['events']=events[-100:]
        final_states=result.pop('final_states',None)
        result['states']=(final_states or
            {task['id']:task['state'] for task in monitor.get('tasks',[])}) if monitor and type(watermark) is int else (
            final_states or {e['task_id']:e['state'] for e in events})
        if result['status']=='interrupted':
            result['states']={k:('interrupted' if v in ('waiting','running') else v) for k,v in result['states'].items()}
        if result.get('preset')=='engineering':
            from .project_workflow import project_view
            result['engineering']=project_view(result['project'],events,result['states'],result['budget'])
        result['agents']=((monitor or {}).get('agents',[]) if result['status'] in ('running','cancelling')
                          else result.get('agent_summary',(monitor or {}).get('agents',[])))
        result['agent_events']=(monitor or {}).get('agent_events',[])
        result['agent_controls']=((monitor or {}).get('agent_controls',[]) if result['status'] in ('running','cancelling')
                                 else result.get('agent_control_summary',(monitor or {}).get('agent_controls',[])))
        return result

    def control_agent(self,key,identity,operation):
        with self.lock:
            if self.recovery_required:raise ValueError(RECOVERY_MESSAGE)
            job=self.jobs.get(key)
            if not job or job['status']!='running' or '_control' not in job:
                raise ValueError('只能控制当前执行中的 Agent；历史记录不会恢复执行')
            return job['_control'].request(identity,operation)


def make_server(controller, host='127.0.0.1', port=18880, proxy_token=''):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(6)
            super().setup()

        def log_message(self,*args):pass

        def respond(self, code, value, kind='application/json; charset=utf-8'):
            data = value.encode() if isinstance(value,str) else json.dumps(value,ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            try:self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError):pass

        def authorized(self):
            if proxy_token and not hmac.compare_digest(self.headers.get('X-AgentRT-Proxy-Token',''),proxy_token):
                self.respond(403,{'error':'需要通过受保护入口访问'})
                return False
            return True

        def do_GET(self):
            if not self.authorized():return
            path=urlparse(self.path).path
            if path=='/':
                self.respond(200,(Path(__file__).parent/'static/index.html').read_text(encoding='utf-8'),'text/html; charset=utf-8')
            elif path in ('/static/console.js','/static/console.css','/static/workspace.css','/static/agents.css'):
                kind='application/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8'
                self.respond(200,(Path(__file__).parent/'static'/path.rsplit('/',1)[1]).read_text(encoding='utf-8'),kind)
            elif path=='/api/status':self.respond(200,controller.snapshot())
            elif path.startswith('/api/jobs/'):
                try:self.respond(200,controller.detail(path.rsplit('/',1)[1]))
                except KeyError:self.respond(404,{'error':'工作流不存在或已移出最近记录'})
            else:self.respond(404,{'error':'未找到'})

        def do_POST(self):
            if not self.authorized():return
            origin=self.headers.get('Origin')
            if (not hmac.compare_digest(self.headers.get('X-AgentRT-CSRF',''),controller.csrf)
                    or (origin and urlparse(origin).netloc != self.headers.get('Host'))):
                self.respond(403,{'error':'请求校验失败，请刷新页面'})
                return
            try:
                self.connection.settimeout(5)
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=20000:raise ValueError('请求体过大或为空')
                value=json.loads(self.rfile.read(length))
                if not isinstance(value,dict):raise ValueError('需要 JSON 对象')
                if self.path=='/api/model-config':
                    key=value.get('key')
                    if not isinstance(key,str) or len(key)>512 or '\n' in key or '\r' in key:
                        raise ValueError('API Key 格式无效')
                    if key and len(key)<16:raise ValueError('API Key 长度不足')
                    with controller.lock:
                        if controller.recovery_required:
                            raise ValueError(RECOVERY_MESSAGE)
                        if any(j['status'] in ('running','cancelling') for j in controller.jobs.values()):
                            raise ValueError('请等待运行结束后再更新模型配置')
                        if controller.model_broker is not None:
                            try:controller.model_broker.configure(key)
                            except ModelError:raise ValueError('模型服务未确认配置，请检查服务状态') from None
                        elif key:os.environ['DEEPSEEK_API_KEY']=key
                        else:os.environ.pop('DEEPSEEK_API_KEY',None)
                    self.respond(200,{'configured':bool(key)})
                elif self.path=='/api/jobs':self.respond(202,{'id':controller.submit(value)})
                elif len(self.path.split('/'))==7 and self.path.startswith('/api/jobs/') and self.path.split('/')[4]=='agents':
                    parts=self.path.split('/')
                    self.respond(202,controller.control_agent(parts[3],parts[5],parts[6]))
                elif self.path.endswith('/cancel') and self.path.startswith('/api/jobs/'):
                    key=self.path.split('/')[-2]
                    with controller.lock:
                        job=controller.jobs.get(key)
                        if not job:raise ValueError('工作流不存在')
                        if job['status']=='running':
                            job['status']='cancelling'
                            job['cancel'].set()
                    self.respond(200,{'ok':True})
                else:self.respond(404,{'error':'未找到'})
            except (ValueError,TypeError,OSError) as exc:self.respond(400,{'error':str(exc)})

    return ThreadingHTTPServer((host,port),Handler)


def delegated_attempts():
    relative=Path('/proc/self/cgroup').read_text().split('0::',1)[1].splitlines()[0]
    root=Path('/sys/fs/cgroup')/relative.lstrip('/')
    control=root/'control'
    control.mkdir(exist_ok=True)
    (control/'cgroup.procs').write_text(str(os.getpid()))
    (root/'cgroup.subtree_control').write_text('+cpu +memory +pids')
    attempts=root/'attempts'
    attempts.mkdir(exist_ok=True)
    (attempts/'cgroup.subtree_control').write_text('+cpu +memory +pids')
    return str(attempts)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=18880)
    parser.add_argument('--data-dir',type=Path,default=Path('web-runs'))
    parser.add_argument('--delegated',action='store_true')
    args=parser.parse_args()
    token=os.environ.get('AGENTRT_PROXY_TOKEN','')
    if args.host not in ('127.0.0.1','localhost') and not token:
        parser.error('非回环监听必须配置 AGENTRT_PROXY_TOKEN')
    attempts=delegated_attempts() if args.delegated else None
    server=make_server(Controller(args.data_dir,attempts),args.host,args.port,token)
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':main()
