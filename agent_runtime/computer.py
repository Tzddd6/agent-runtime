"""Bounded NUC computer session, a child of the admitted Runtime Attempt."""
import json,os,signal,subprocess,sys
from pathlib import Path

def python_path():
    return Path.home()/'.local/share/agentrt-browser-venv/bin/python'

def available():return os.name=='posix' and python_path().is_file() and (Path.home()/'.local/share/agentrt-browser-ready.json').is_file()

def tasks(prompt,url=None,max_steps=8):
    return [{'id':'auto_answer','agent':'computer-operator','action':'plugin','plugin':'computer.run',
        'arguments':{'goal':prompt,'url':url or 'https://agentrt-lab.invalid/','max_steps':max_steps},'timeout':175,
        'resources':{'cpu_milli':1000,'memory_bytes':1024**3,'pids':224}}]

def execute(task,args):
    if set(args)-{'goal','url','max_steps'} or not isinstance(args.get('goal'),str) or not 1<=len(args['goal'])<=12000:raise ValueError('电脑任务需要1—12000字goal')
    steps=args.get('max_steps',8)
    if type(steps) is not int or not 1<=steps<=12:raise ValueError('电脑任务最多12步')
    if not available():raise ValueError('专用电脑实验环境尚未安装；没有连接个人桌面')
    if not task.get('_gateway_url') or not task.get('_gateway_token'):raise ValueError('电脑Bot需要Runtime统一模型准入')
    payload={k:task[k] for k in ('_workspace','_project_workspace','_gateway_url','_gateway_token') if k in task}
    payload['arguments']=args
    proc=subprocess.Popen([str(python_path()),'-I',str(Path(__file__).with_name('computer_worker.py'))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        env={'PATH':'/usr/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'},start_new_session=True)
    try:
        raw,_=proc.communicate(json.dumps(payload,ensure_ascii=False).encode(),timeout=165)
        if proc.returncode or len(raw)>256*1024:raise ValueError('电脑执行端未正常结束；未自动重放操作')
        result=json.loads(raw)
        if not isinstance(result,dict):raise ValueError('电脑执行端返回结构无效')
        return {'plugin':'computer.run',**result}
    except subprocess.TimeoutExpired:raise ValueError('电脑会话达到总期限，已停止；未自动重试') from None
    finally:
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        proc.wait(timeout=3)
