"""受保护的轻量 Runtime 控制台，仅接受固定演示或模型对话，不开放任意命令。"""
import argparse
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from .runtime import run


DEMO = Path(__file__).resolve().parents[1] / 'examples/dynamic.json'


class Controller:
    def __init__(self, root, cgroup_root=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cgroup_root = cgroup_root
        self.csrf = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.jobs = {}

    def submit(self, value):
        preset = value.get('preset')
        workers = value.get('workers', 2)
        policy = value.get('policy', 'resource_fit')
        if type(workers) is not int or not 1 <= workers <= 4 or policy not in ('fifo', 'resource_fit'):
            raise ValueError('并发数需为 1—4，调度策略不合法')
        if preset == 'dynamic':
            tasks = json.loads(DEMO.read_text(encoding='utf-8'))
        elif preset == 'ipc':
            tasks = json.loads((DEMO.parent/'ipc.json').read_text(encoding='utf-8'))
        elif preset == 'document':
            from .scenarios import tasks_for
            text = value.get('prompt', '') or 'Agent Runtime shares context. Agent Runtime schedules tasks.'
            if not isinstance(text, str) or len(text) > 4000:
                raise ValueError('文档长度不得超过 4000 字')
            tasks = tasks_for(text)
        elif preset in ('llm','team'):
            if not os.environ.get('DEEPSEEK_API_KEY'):
                raise ValueError('服务端尚未配置 DEEPSEEK_API_KEY')
            prompt = value.get('prompt', '')
            if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
                raise ValueError('请输入 1—4000 字的提示词')
            tasks = [{'id':'assistant', 'agent':'assistant', 'action':'llm', 'timeout':75,
                      'max_tokens':512, 'messages':[{'role':'user','content':prompt}]}]
            if preset == 'team':
                from .scenarios import team_tasks
                tasks = team_tasks(prompt)
        else:
            raise ValueError('只能运行动态演示或模型对话，不接受任意任务/命令')
        with self.lock:
            if any(j['status'] in ('running','cancelling') for j in self.jobs.values()):
                raise ValueError('已有工作流运行中，请等待结束或停止后再提交')
            key = uuid.uuid4().hex
            job = {'id':key,'preset':preset,'status':'running','created':time.time(),
                   'cancel':threading.Event(), 'directory':self.root/key}
            self.jobs[key] = job
            # 内存索引保留最近 20 次，磁盘结果保留供团队管理。
            for old in list(self.jobs)[:-20]:
                if self.jobs[old]['status'] not in ('running','cancelling'):
                    del self.jobs[old]

        def execute():
            try:
                states, _ = run(tasks, workers, 75, output_dir=job['directory'], policy=policy,
                                cgroup_root=self.cgroup_root, cancel_event=job['cancel'], quiet=True)
                with self.lock:
                    job['status'] = ('cancelled' if any(s=='cancelled' for s in states.values())
                                     else 'succeeded' if all(s=='succeeded' for s in states.values()) else 'failed')
            except Exception:
                with self.lock:
                    job['status']='failed'
                    job['error']='运行失败，请管理员查看服务日志及委派配置'
            finally:
                with self.lock:
                    job['finished']=time.time()

        threading.Thread(target=execute,daemon=True).start()
        return key

    def snapshot(self):
        with self.lock:
            jobs = [{k:v for k,v in j.items() if k not in ('cancel','directory')} for j in self.jobs.values()]
        return {'jobs':list(reversed(jobs)), 'csrf':self.csrf,
                'model_configured':bool(os.environ.get('DEEPSEEK_API_KEY')),
                'model':os.environ.get('DEEPSEEK_MODEL','deepseek-flash'),
                'enforcement':'cgroup_v2' if self.cgroup_root else 'admission_only'}

    def detail(self, key):
        with self.lock:
            job = self.jobs.get(key)
            if not job:
                raise KeyError(key)
            result = {k:v for k,v in job.items() if k not in ('cancel','directory')}
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
        result['events']=events[-100:]
        result['states']={e['task_id']:e['state'] for e in events}
        return result


def make_server(controller, host='127.0.0.1', port=18880, proxy_token=''):
    class Handler(BaseHTTPRequestHandler):
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
                        if any(j['status'] in ('running','cancelling') for j in controller.jobs.values()):
                            raise ValueError('请等待运行结束后再更新模型配置')
                        if key:os.environ['DEEPSEEK_API_KEY']=key
                        else:os.environ.pop('DEEPSEEK_API_KEY',None)
                    self.respond(200,{'configured':bool(key)})
                elif self.path=='/api/jobs':self.respond(202,{'id':controller.submit(value)})
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
