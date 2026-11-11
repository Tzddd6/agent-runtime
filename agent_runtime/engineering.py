"""Three small real projects and intentionally restricted patch checkers.

Only fixed integer/string Python subsets are executable. This is not a sandbox
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


def project_ids():
    return ('pricing','intervals','text_normalization')


def _spec(project):
    if not isinstance(project,str) or project not in PROJECTS:
        raise ValueError('unknown controlled project')
    return PROJECTS[project]


def project_metadata(project=None):
    def metadata(key):
        spec=_spec(key)
        return {'id':key,'name':spec['name'],'file':spec['file'],
                'public_tests':len(spec['public']),'heldout_tests':len(spec['heldout'])}
    return metadata(project) if project is not None else [metadata(key) for key in project_ids()]


def manifest(project='pricing'):
    spec=_spec(project)
    return {'project':spec['name'],'file':spec['file'],'requirements':spec['requirements'],
            'source':spec['source'],'public_examples':spec['examples'],
            'patch_schema':{'source':'complete '+spec['file']+' source; JSON only'}}


def project_context(project='pricing'):
    return json.dumps(manifest(project),ensure_ascii=False,separators=(',',':'))


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
STRING_METHODS={'strip','lower','split','join','startswith'}


def _string_type(node,types):
    if isinstance(node,ast.Name):return types.get(node.id)
    if isinstance(node,ast.Constant):return 'str' if type(node.value) is str else 'int'
    if isinstance(node,ast.Call):
        if isinstance(node.func,ast.Name):return 'int' if node.func.id=='len' else None
        if isinstance(node.func,ast.Attribute) and _string_type(node.func.value,types)=='str':
            return {'strip':'str','lower':'str','split':'list_str','join':'str','startswith':'bool'}.get(node.func.attr)
    return None


def _check_string_call(node,types):
    method=node.func.attr
    if (method not in STRING_METHODS or _string_type(node.func.value,types)!='str'
            or node.keywords):
        raise ValueError('only methods on known string inputs or safe string chains are allowed')
    if method=='lower':valid=len(node.args)==0
    elif method in ('strip','split'):
        valid=len(node.args)<=1 and all(_string_type(arg,types)=='str' for arg in node.args)
    elif method=='startswith':valid=len(node.args)==1 and _string_type(node.args[0],types)=='str'
    else:valid=len(node.args)==1 and _string_type(node.args[0],types) in ('str','list_str')
    if not valid:raise ValueError('unsupported safe string method arguments')


def validate_patch(raw,project='pricing'):
    spec=_spec(project)
    signatures=spec['signatures']
    text_project=project=='text_normalization'
    safe_calls=SAFE_CALLS|{'len'} if text_project else SAFE_CALLS
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
        raise ValueError('source exceeds source limit')
    try:tree=ast.parse(source,filename=spec['file'])
    except (SyntaxError,ValueError,RecursionError,MemoryError) as exc:
        raise ValueError('invalid '+spec['file']+' syntax') from exc
    nodes=list(ast.walk(tree))
    allowed=ALLOWED+(ast.Attribute,) if text_project else ALLOWED
    if len(nodes)>700 or any(not isinstance(node,allowed) for node in nodes):
        raise ValueError('source is outside the allowed fixed-project Python subset')
    if len(tree.body)!=3 or any(not isinstance(node,ast.FunctionDef) for node in tree.body):
        raise ValueError('module must contain exactly the three specified functions')
    if {node.name for node in tree.body}!=set(signatures):
        raise ValueError('function names must match the fixed project')
    for function in tree.body:
        args=function.args
        if (tuple(arg.arg for arg in args.args)!=signatures[function.name]
                or args.posonlyargs or args.vararg or args.kwarg or args.kwonlyargs
                or args.defaults or args.kw_defaults or function.decorator_list
                or function.returns is not None or getattr(function,'type_params',[])
                or any(arg.annotation is not None for arg in args.args)):
            raise ValueError('function signatures/decorators/annotations cannot change')
        locals_=set(signatures[function.name])
        types={name:'str' for name in locals_} if text_project else {}
        assignments=[]
        for node in ast.walk(function):
            if isinstance(node,ast.FunctionDef) and node is not function:
                raise ValueError('nested functions are forbidden')
            if isinstance(node,ast.Assign):
                if len(node.targets)!=1 or not isinstance(node.targets[0],ast.Name):
                    raise ValueError('only single local-variable assignments are supported')
                name=node.targets[0].id
                if name in SAFE_CALLS|{'len'} or name.startswith('_') or '__' in name:
                    raise ValueError('reserved local name')
                locals_.add(name)
                assignments.append((name,node.value))
        if text_project:
            for _ in range(len(assignments)+1):
                changed=False
                for name in {name for name,_ in assignments}:
                    inferred={_string_type(value,types) for target,value in assignments if target==name}
                    if len(inferred)==1 and None not in inferred and types.get(name)!=next(iter(inferred)):
                        types[name]=next(iter(inferred));changed=True
                if not changed:break
        method_attributes={id(n.func) for n in ast.walk(function)
                           if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)}
        for node in ast.walk(function):
            if isinstance(node,ast.Name):
                if node.id.startswith('_') or '__' in node.id or node.id not in locals_|safe_calls:
                    raise ValueError('unknown or reserved name')
            elif isinstance(node,ast.Constant):
                if not ((type(node.value) is int and abs(node.value)<=10**9)
                        or (type(node.value) is str and len(node.value)<=200)
                        or (text_project and type(node.value) is bool)):
                    raise ValueError('unsupported literal')
            elif isinstance(node,ast.Expr):
                if not isinstance(node.value,ast.Constant) or not isinstance(node.value.value,str):
                    raise ValueError('only documentation strings are permitted as expression statements')
            elif isinstance(node,ast.Call):
                if text_project and isinstance(node.func,ast.Attribute):
                    _check_string_call(node,types)
                    continue
                if text_project and isinstance(node.func,ast.Name) and node.func.id=='len':
                    if node.keywords or len(node.args)!=1 or _string_type(node.args[0],types) not in ('str','list_str'):
                        raise ValueError('len only accepts safe strings or split results')
                    continue
                if (not isinstance(node.func,ast.Name) or node.func.id not in safe_calls
                        or node.keywords or not 1<=len(node.args)<=3
                        or (node.func.id=='ValueError' and (len(node.args)!=1
                            or not isinstance(node.args[0],ast.Constant)
                            or not isinstance(node.args[0].value,str)))):
                    raise ValueError('only constrained ValueError/min/max calls are allowed')
            elif isinstance(node,ast.Attribute):
                if not text_project or id(node) not in method_attributes or node.attr not in STRING_METHODS:
                    raise ValueError('arbitrary attribute access and method aliases are forbidden')
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

INTERVAL_SIGNATURES={
    'overlap_seconds':('start_a','end_a','start_b','end_b'),
    'bucket_index':('timestamp','origin','width'),
    'remaining_ttl':('now','created','ttl')}
INTERVAL_SOURCE='''def overlap_seconds(start_a, end_a, start_b, end_b):
    if end_a < start_a or end_b < start_b:
        raise ValueError("invalid")
    return min(end_a, end_b) - max(start_a, start_b)
def bucket_index(timestamp, origin, width):
    if width <= 0:
        raise ValueError("invalid")
    return timestamp // width - origin // width
def remaining_ttl(now, created, ttl):
    if ttl < 0:
        raise ValueError("invalid")
    return max(0, ttl - (now - created))
'''
INTERVAL_PUBLIC=[
 ('overlap_seconds',(0,3,5,8),0),('overlap_seconds',(0,5,3,8),2),
 ('overlap_seconds',(4,1,0,3),'ValueError'),
 ('bucket_index',(10,3,4),1),('bucket_index',(-1,0,5),-1),('bucket_index',(1,0,0),'ValueError'),
 ('remaining_ttl',(8,10,5),5),('remaining_ttl',(12,10,5),3),('remaining_ttl',(20,10,5),0)]
INTERVAL_HELDOUT=[
 ('overlap_seconds',(-5,-1,-3,4),2),('overlap_seconds',(0,3,3,5),0),
 ('overlap_seconds',(1,1,0,5),0),('overlap_seconds',(0,5,0,5),5),
 ('overlap_seconds',(0,5,9,8),'ValueError'),
 ('bucket_index',(2,3,4),-1),('bucket_index',(3,3,4),0),('bucket_index',(-5,-3,4),-1),
 ('bucket_index',(5,2,-1),'ValueError'),
 ('remaining_ttl',(0,0,0),0),('remaining_ttl',(9,10,0),0),
 ('remaining_ttl',(-3,-5,7),5),('remaining_ttl',(5,3,-1),'ValueError')]
TEXT_SIGNATURES={'normalize_label':('text',),'word_count':('text',),'starts_with':('text','prefix')}
TEXT_SOURCE='''def normalize_label(text):
    return text.strip().lower()
def word_count(text):
    return len(text.split(" "))
def starts_with(text, prefix):
    return text.startswith(prefix)
'''
TEXT_PUBLIC=[
 ('normalize_label',('  Hello   WORLD  ',),'hello world'),
 ('normalize_label',('A\tB\nC',),'a b c'),
 ('word_count',('',),0),('word_count',('one  two',),2),('word_count',('one\ttwo',),2),
 ('starts_with',('  Hello World',' HELLO '),True),
 ('starts_with',('Alpha','beta'),False),('starts_with',(' ',' '),True)]
TEXT_HELDOUT=[
 ('normalize_label',('',),''),('normalize_label',(' \t\n ',),''),
 ('normalize_label',('MiXeD',),'mixed'),('normalize_label',(' Ä\u2003B ',),'ä b'),
 ('word_count',(' \t\n ',),0),('word_count',('a\nb\tc',),3),
 ('word_count',('word',),1),('word_count',(' x ',),1),
 ('starts_with',('  PrefixSuffix\n','prefix'),True),('starts_with',('anything',''),True),
 ('starts_with',('','anything'),False),('starts_with',('  ÄBC',' ä '),True),
 ('starts_with',('foo','FOOBAR'),False)]
PROJECTS={
 'pricing':{'name':'integer-pricing','file':'pricing.py','source':BASELINE_SOURCE,
   'requirements':REQUIREMENTS,'signatures':SIGNATURES,'public':PUBLIC_CASES,'heldout':HELDOUT_CASES,
   'examples':[['line_total_cents',[199,3,10],537],['shipping_cents',[5000],0],['refund_cents',[100,2,3],66]]},
 'intervals':{'name':'time-intervals','file':'intervals.py','source':INTERVAL_SOURCE,
   'signatures':INTERVAL_SIGNATURES,'public':INTERVAL_PUBLIC,'heldout':INTERVAL_HELDOUT,
   'requirements':('All inputs are integers. overlap_seconds: length of intersection of half-open intervals; '
     'reject end<start; non-overlap or empty interval gives zero. '
     'bucket_index: floor((timestamp-origin)/width), including negatives; width must be positive. '
     'remaining_ttl: ttl minus elapsed time, clamped to 0..ttl; future-created item retains ttl; reject ttl<0. '
     'Invalid values raise ValueError. Keep three signatures. Only integer arithmetic //, comparisons, if/return, '
     'ValueError/min/max; no imports, attributes, loops or other calls.'),
   'examples':[['overlap_seconds',[0,3,5,8],0],['bucket_index',[10,3,4],1],['remaining_ttl',[8,10,5],5]]},
 'text_normalization':{'name':'text-normalization','file':'text_utils.py','source':TEXT_SOURCE,
   'signatures':TEXT_SIGNATURES,'public':TEXT_PUBLIC,'heldout':TEXT_HELDOUT,
   'requirements':('All arguments are strings. normalize_label: split on arbitrary Unicode whitespace, '
     'join words with one ASCII space and lowercase. word_count: count whitespace-separated words; empty/whitespace-only gives zero. '
     'starts_with: trim and lowercase both text and prefix, then return bool for prefix match; empty prefix is true. '
     'Keep three signatures. Only safe str methods strip/lower/split/join/startswith and len; safe method chains and local variables allowed. '
     'No imports, subscripts, loops, dunder, reflection, function aliases or arbitrary calls.'),
   'examples':[['normalize_label',['  Hello   WORLD  '],'hello world'],['word_count',['one  two'],2],['starts_with',['  Hi',' HI '],True]]}
}

RUNNER = '''import io,json,sys,unittest
from pathlib import Path
source=Path(sys.argv[1]).read_text(encoding='utf-8')
scope={'__builtins__':{'ValueError':ValueError,'min':min,'max':max,'len':len}}
exec(compile(source,Path(sys.argv[1]).name,'exec'),scope,scope)
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
                case.assertIs(type(value),type(expected))
                case.assertEqual(value,expected)
        suite.addTest(unittest.FunctionTestCase(check,description=group+':'+str(index)))
    result=unittest.TextTestRunner(stream=stream,verbosity=1).run(suite)
    report[group]={'total':result.testsRun,'passed':result.testsRun-len(result.failures)-len(result.errors),
                   'failed':len(result.failures),'errors':len(result.errors),'stdout':stream.getvalue()}
print(json.dumps(report))
'''


def _execute(source,directory,runner,cases,filename='pricing.py'):
    directory.mkdir(mode=0o700)
    target=directory/filename
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


def run_check(source,workspace,project='pricing'):
    spec=_spec(project)
    source=validate_patch({'source':source},project)
    workspace=Path(workspace)
    if workspace.is_symlink():raise ValueError('workspace cannot be a symlink')
    workspace.mkdir(parents=True,exist_ok=True)
    if not workspace.is_dir():raise ValueError('workspace must be a directory')
    root=Path(tempfile.mkdtemp(prefix=project+'-',dir=workspace.resolve()))
    runner=root/'trusted_runner.py'; runner.write_text(RUNNER,encoding='utf-8')
    cases=root/'immutable_cases.json'
    cases.write_text(json.dumps({'public':spec['public'],'heldout':spec['heldout']}),encoding='utf-8')
    baseline=_execute(spec['source'],root/'baseline',runner,cases,spec['file'])
    candidate=_execute(source,root/'candidate',runner,cases,spec['file'])
    baseline_failing=sum(baseline[group]['failed']+baseline[group]['errors'] for group in ('public','heldout'))
    report={'project':spec['name'],'project_id':project,'success':candidate['success'],
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
    parser.add_argument('--project',choices=project_ids(),default='pricing')
    parser.add_argument('--workspace',type=Path,default=Path('runs/engineering'))
    args=parser.parse_args()
    if args.action=='context':print(project_context(args.project));return 0
    if args.action=='check':
        if args.response is None:parser.error('--response is required')
        with args.response.open('rb') as source:raw=source.read(32769)
        if len(raw)>32768:parser.error('response exceeds 32 KiB')
        source=validate_patch(raw.decode('utf-8'),args.project)
    else:source=_spec(args.project)['source']
    report=run_check(source,args.workspace,args.project)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['success'] else 1


if __name__=='__main__':raise SystemExit(main())
