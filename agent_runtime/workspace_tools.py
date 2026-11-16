"""Bounded Python project snapshots, protected-test patches and isolated tests."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from .tooling import workspace,operate,scoped_path,workspace_usage


def protected(name):
    p=Path(name)
    return any(part in ('tests','test') for part in p.parts) or p.name.startswith('test') or p.name.endswith('_test.py')


def inspect(task):
    root=workspace(task);workspace_usage(root);files=[];total=0
    for p in sorted(root.rglob('*.py')):
        if any(part.startswith('.') or part in ('__pycache__','verified') for part in p.relative_to(root).parts):continue
        name=p.relative_to(root).as_posix();scoped_path(root,name)
        value=operate({**task,'_download_full':True},'file.read',{'path':name})
        total+=len(value['content'].encode())
        if len(files)>=32 or total>160000:raise ValueError('工程快照限32个Python文件、160KB；请缩小工作目录')
        files.append({**value,'protected_test':protected(name)})
    return {'files':files,'source_count':sum(not f['protected_test'] for f in files),
            'test_count':sum(f['protected_test'] for f in files),'executed':True,'scope':'current_workspace'}


def apply_patch(task,args):
    patches=args.get('patches')
    if set(args)!={'patches'} or not isinstance(patches,list) or not 1<=len(patches)<=6:raise ValueError('patches需为1—6项')
    prepared=[];seen=set()
    for patch in patches:
        if not isinstance(patch,dict) or set(patch)!={'path','sha256','content'}:raise ValueError('补丁需包含path、sha256、content')
        name=patch['path'];scoped_path(workspace(task),name)
        if name in seen or protected(name) or not name.endswith('.py'):raise ValueError('只可修改已有Python源码，禁止测试或重复路径')
        seen.add(name)
        if not isinstance(patch['content'],str) or len(patch['content'].encode())>40000:raise ValueError('单个补丁超过40KB')
        compile(patch['content'],name,'exec')
        original=operate({**task,'_download_full':True},'file.read',{'path':name})
        if original['sha256']!=patch['sha256']:raise ValueError('源码已变化；未应用任何补丁')
        prepared.append((patch,original))
    written=[]
    try:
        for patch,original in prepared:
            result=operate(task,'file.write',{'path':patch['path'],'content':patch['content']})
            written.append((result,original))
    except BaseException:
        for result,original in reversed(written):operate(task,'file.write',{'path':original['path'],'content':original['content']})
        raise
    return {'files':[r for r,_ in written],'written':True,'executed':True,'tests_modified':False,
            'atomicity':'prevalidated_under_workspace_lock_with_failure_rollback; not crash-atomic'}


TEST_RUNNER='''import unittest,sys
suite=unittest.defaultTestLoader.discover('.',pattern='test*.py')
count=suite.countTestCases()
if count<1:
 print('AGENTRT_TEST_COUNT=0');sys.exit(3)
result=unittest.TextTestRunner(verbosity=1).run(suite)
print('AGENTRT_TEST_COUNT='+str(count))
sys.exit(0 if result.wasSuccessful() and len(result.skipped)<count else 4)
'''


def test(task):
    before=inspect(task);hashes={f['path']:f['sha256'] for f in before['files'] if f['protected_test']}
    result=operate(task,'command.run',{'argv':['python3','-B','-c',TEST_RUNNER],'timeout':15})
    after=inspect(task);after_hashes={f['path']:f['sha256'] for f in after['files'] if f['protected_test']}
    count=re.findall(r'^AGENTRT_TEST_COUNT=(\d+)$',result['output'],re.M)
    unchanged=hashes==after_hashes
    return {**result,'success':result['ok'] and unchanged and bool(count) and int(count[-1])>0,
            'test_cases':int(count[-1]) if count else None,'tests_modified':not unchanged,
            'test_hashes':after_hashes,'source_hashes':{f['path']:f['sha256'] for f in after['files'] if not f['protected_test']},
            'output':result['output'][-10000:]}


def execute(task,name,args):
    if name=='workspace.inspect' and not args:return inspect(task)
    if name=='workspace.test' and not args:return test(task)
    if name=='workspace.apply_patch':return apply_patch(task,args)
    raise ValueError('工程工具参数无效')
