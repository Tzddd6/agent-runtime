"""A small real pricing project and an intentionally restricted patch checker.

Only this fixed pure-integer Python subset is executable. This is not a sandbox
for arbitrary repositories or arbitrary Python; use an outer delegated cgroup.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


SIGNATURES = {
    'line_total_cents': ('unit_cents','quantity','discount_pct'),
    'shipping_cents': ('subtotal_cents',),
    'refund_cents': ('paid_cents','returned_qty','total_qty'),
}
BASELINE_SOURCE = '''def line_total_cents(unit_cents, quantity, discount_pct):
    if unit_cents < 0 or quantity < 0 or discount_pct < 0 or discount_pct > 100:
        raise ValueError("invalid")
    return unit_cents * quantity - discount_pct

def shipping_cents(subtotal_cents):
    if subtotal_cents < 0:
        raise ValueError("invalid")
    if subtotal_cents > 5000:
        return 0
    return 599

def refund_cents(paid_cents, returned_qty, total_qty):
    if paid_cents < 0 or total_qty <= 0 or returned_qty < 0 or returned_qty > total_qty:
        raise ValueError("invalid")
    return paid_cents // total_qty * returned_qty
'''
REQUIREMENTS = (
    'Inputs are integers; money is non-negative cents. '
    'line_total_cents(unit_cents,quantity,discount_pct): unit_cents and quantity >=0, '
    'discount_pct in 0..100; apply percentage discount to the whole line and floor once '
    'to integer cents. shipping_cents(subtotal_cents): reject negatives, '
    'free shipping for subtotal >=5000, otherwise 599. '
    'refund_cents(paid_cents,returned_qty,total_qty): paid_cents>=0, total_qty>0, '
    '0<=returned_qty<=total_qty; prorate the whole paid amount and floor once. '
    'Invalid values must raise ValueError. Keep exactly these three signatures. '
    'No imports, attributes, subscripts, loops, decorators or arbitrary calls; '
    'only integer arithmetic, comparisons, if/return, and ValueError/min/max are supported.'
)


def manifest():
    return {'project':'integer-pricing','file':'pricing.py','requirements':REQUIREMENTS,
            'source':BASELINE_SOURCE,
            'public_examples':[
                ['line_total_cents',[199,3,10],537],
                ['shipping_cents',[5000],0],
                ['refund_cents',[100,2,3],66]],
            'patch_schema':{'source':'complete pricing.py source; JSON only'}}


def project_context():
    return json.dumps(manifest(),ensure_ascii=False,separators=(',',':'))


def _json_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate patch field')
        result[key]=value
    return result


def _non_json_constant(_):
    raise ValueError('non-JSON constant')


ALLOWED = (ast.Module,ast.FunctionDef,ast.arguments,ast.arg,ast.Return,ast.Assign,
           ast.If,ast.Raise,ast.Expr,ast.Constant,ast.Name,ast.Load,ast.Store,
           ast.BinOp,ast.Add,ast.Sub,ast.Mult,ast.FloorDiv,ast.Mod,ast.UnaryOp,
           ast.USub,ast.UAdd,ast.Not,ast.BoolOp,ast.And,ast.Or,ast.Compare,
           ast.Eq,ast.NotEq,ast.Lt,ast.LtE,ast.Gt,ast.GtE,ast.Call)
SAFE_CALLS={'ValueError','min','max'}


def validate_patch(raw):
    if isinstance(raw,str):
        if len(raw.encode('utf-8'))>32768:raise ValueError('patch exceeds 32 KiB')
        try:
            value=json.loads(raw,object_pairs_hook=_json_object,parse_constant=_non_json_constant)
        except (json.JSONDecodeError,RecursionError) as exc:
            raise ValueError('patch must be a single JSON object') from exc
    else:value=raw
    if not isinstance(value,dict) or set(value)!={'source'} or not isinstance(value['source'],str):
        raise ValueError('only the source field is accepted; file paths and test edits are forbidden')
    source=value['source'].strip()+'\n'
    if len(source.encode('utf-8'))>8192 or len(source.splitlines())>120:
        raise ValueError('pricing.py exceeds source limit')
    try:tree=ast.parse(source,filename='pricing.py')
    except (SyntaxError,ValueError,RecursionError,MemoryError) as exc:
        raise ValueError('invalid pricing.py syntax') from exc
    nodes=list(ast.walk(tree))
    if len(nodes)>700 or any(not isinstance(node,ALLOWED) for node in nodes):
        raise ValueError('source is outside the allowed pure-integer subset')
    if len(tree.body)!=3 or any(not isinstance(node,ast.FunctionDef) for node in tree.body):
        raise ValueError('module must contain exactly the three specified functions')
    if {node.name for node in tree.body}!=set(SIGNATURES):
        raise ValueError('function names must match the fixed project')
    for function in tree.body:
        args=function.args
        if (tuple(arg.arg for arg in args.args)!=SIGNATURES[function.name]
                or args.posonlyargs or args.vararg or args.kwarg or args.kwonlyargs
                or args.defaults or args.kw_defaults or function.decorator_list
                or function.returns is not None or getattr(function,'type_params',[])
                or any(arg.annotation is not None for arg in args.args)):
            raise ValueError('function signatures/decorators/annotations cannot change')
        locals_=set(SIGNATURES[function.name])
        for node in ast.walk(function):
            if isinstance(node,ast.FunctionDef) and node is not function:
                raise ValueError('nested functions are forbidden')
            if isinstance(node,ast.Assign):
                if len(node.targets)!=1 or not isinstance(node.targets[0],ast.Name):
                    raise ValueError('only single local-variable assignments are supported')
                name=node.targets[0].id
                if name in SAFE_CALLS or name.startswith('_') or '__' in name:
                    raise ValueError('reserved local name')
                locals_.add(name)
        for node in ast.walk(function):
            if isinstance(node,ast.Name):
                if node.id.startswith('_') or '__' in node.id or node.id not in locals_|SAFE_CALLS:
                    raise ValueError('unknown or reserved name')
            elif isinstance(node,ast.Constant):
                if not ((type(node.value) is int and abs(node.value)<=10**9)
                        or (type(node.value) is str and len(node.value)<=200)):
                    raise ValueError('unsupported literal')
            elif isinstance(node,ast.Expr):
                if not isinstance(node.value,ast.Constant) or not isinstance(node.value.value,str):
                    raise ValueError('only documentation strings are permitted as expression statements')
            elif isinstance(node,ast.Call):
                if (not isinstance(node.func,ast.Name) or node.func.id not in SAFE_CALLS
                        or node.keywords or not 1<=len(node.args)<=3
                        or (node.func.id=='ValueError' and (len(node.args)!=1
                            or not isinstance(node.args[0],ast.Constant)
                            or not isinstance(node.args[0].value,str)))):
                    raise ValueError('only constrained ValueError/min/max calls are allowed')
            elif isinstance(node,ast.Raise):
                if (node.cause is not None or not isinstance(node.exc,ast.Call)
                        or not isinstance(node.exc.func,ast.Name) or node.exc.func.id!='ValueError'):
                    raise ValueError('only ValueError may be raised')
    return source


# Kept out of manifest()/project_context(): these are independent checking cases,
# not suggested answers or source supplied to the model.
PUBLIC_CASES = [
    ('line_total_cents',(199,3,10),537),('line_total_cents',(100,2,0),200),
    ('line_total_cents',(-1,2,5),'ValueError'),
    ('shipping_cents',(4999,),599),('shipping_cents',(5000,),0),('shipping_cents',(-1,),'ValueError'),
    ('refund_cents',(100,2,3),66),('refund_cents',(100,3,3),100),
    ('refund_cents',(100,4,3),'ValueError')]
HELDOUT_CASES = [
    ('line_total_cents',(1,3,50),1),('line_total_cents',(999,0,25),0),
    ('line_total_cents',(999,7,100),0),('line_total_cents',(101,9,33),609),
    ('line_total_cents',(10,1,101),'ValueError'),('line_total_cents',(10,-1,0),'ValueError'),
    ('shipping_cents',(0,),599),('shipping_cents',(5001,),0),
    ('shipping_cents',(50000,),0),('shipping_cents',(-5000,),'ValueError'),
    ('refund_cents',(101,2,3),67),('refund_cents',(7,7,7),7),
    ('refund_cents',(999,0,4),0),('refund_cents',(1,1,0),'ValueError'),
    ('refund_cents',(-1,1,1),'ValueError'),('refund_cents',(99,-1,3),'ValueError')]

RUNNER = '''import io,json,sys,unittest
from pathlib import Path
source=Path(sys.argv[1]).read_text(encoding='utf-8')
scope={'__builtins__':{'ValueError':ValueError,'min':min,'max':max}}
exec(compile(source,'pricing.py','exec'),scope,scope)
cases=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
report={}
for group,rows in cases.items():
    stream=io.StringIO()
    suite=unittest.TestSuite()
    for index,(name,args,expected) in enumerate(rows):
        def check(name=name,args=args,expected=expected):
            case=unittest.TestCase()
            if expected=='ValueError':
                with case.assertRaises(ValueError):scope[name](*args)
            else:
                value=scope[name](*args)
                case.assertIs(type(value),int)
                case.assertEqual(value,expected)
        suite.addTest(unittest.FunctionTestCase(check,description=group+':'+str(index)))
    result=unittest.TextTestRunner(stream=stream,verbosity=1).run(suite)
    report[group]={'total':result.testsRun,'passed':result.testsRun-len(result.failures)-len(result.errors),
                   'failed':len(result.failures),'errors':len(result.errors),'stdout':stream.getvalue()}
print(json.dumps(report))
'''


def _execute(source,directory,runner,cases):
    directory.mkdir(mode=0o700)
    target=directory/'pricing.py'
    target.write_text(source,encoding='utf-8')
    environment={key:os.environ[key] for key in ('PATH','SystemRoot','WINDIR','TEMP','TMP') if key in os.environ}
    started=time.perf_counter()
    try:
        process=subprocess.run([sys.executable,'-I','-S','-X','utf8',str(runner),str(target),str(cases)],
            cwd=directory,env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,text=True,encoding='utf-8',timeout=5)
        if process.returncode:raise ValueError('trusted checker failed: '+process.stderr[-4000:])
        value=json.loads(process.stdout)
        value['success']=all(value[group]['failed']==value[group]['errors']==0 for group in ('public','heldout'))
        value['timed_out']=False
        value['wall_seconds']=time.perf_counter()-started
        value['exit_code']=process.returncode
        return value
    except subprocess.TimeoutExpired:
        return {'success':False,'timed_out':True,'public':None,'heldout':None,
                'wall_seconds':time.perf_counter()-started,'exit_code':None}


def run_check(source,workspace):
    source=validate_patch({'source':source})
    workspace=Path(workspace)
    if workspace.is_symlink():raise ValueError('workspace cannot be a symlink')
    workspace.mkdir(parents=True,exist_ok=True)
    if not workspace.is_dir():raise ValueError('workspace must be a directory')
    root=Path(tempfile.mkdtemp(prefix='pricing-',dir=workspace.resolve()))
    runner=root/'trusted_runner.py'; runner.write_text(RUNNER,encoding='utf-8')
    cases=root/'immutable_cases.json'
    cases.write_text(json.dumps({'public':PUBLIC_CASES,'heldout':HELDOUT_CASES}),encoding='utf-8')
    baseline=_execute(BASELINE_SOURCE,root/'baseline',runner,cases)
    candidate=_execute(source,root/'candidate',runner,cases)
    baseline_failing=sum(baseline[group]['failed']+baseline[group]['errors'] for group in ('public','heldout'))
    report={'project':'integer-pricing','success':candidate['success'],
            'source_sha256':hashlib.sha256(source.encode()).hexdigest(),
            'baseline_failing_count':baseline_failing,'baseline':baseline,
            'public':candidate['public'],'heldout':candidate['heldout'],
            'candidate':candidate,'workspace':str(root),'report_path':str(root/'report.json'),
            'stdout':'\n'.join(candidate[group]['stdout'] for group in ('public','heldout')) if not candidate['timed_out'] else 'checker timed out'}
    (root/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('context','baseline','check'))
    parser.add_argument('--response',type=Path)
    parser.add_argument('--workspace',type=Path,default=Path('runs/engineering'))
    args=parser.parse_args()
    if args.action=='context':print(project_context());return 0
    if args.action=='check':
        if args.response is None:parser.error('--response is required')
        with args.response.open('rb') as source:raw=source.read(32769)
        if len(raw)>32768:parser.error('response exceeds 32 KiB')
        source=validate_patch(raw.decode('utf-8'))
    else:source=BASELINE_SOURCE
    report=run_check(source,args.workspace)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['success'] else 1


if __name__=='__main__':raise SystemExit(main())
