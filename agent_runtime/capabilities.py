"""Curated local tool/skill registry. No dynamic imports, shell, network or paths."""
import ast
import csv
import difflib
import io
import json
import math
from pathlib import Path
import re

SKILL_ROOT=Path(__file__).resolve().parent/'skills'
SKILLS={'python-review':'检查Python接口、边界与错误处理',
        'benchmark-evaluation':'冻结数据与判分器，同预算对照并保留失败',
        'streaming-review':'检查公式、代码、表格和长回答的增量与最终一致性',
        'computer-lab':'在专用实验电脑观察页面、操作并验证，拒绝凭据与高风险外发',
        'test-design':'设计正常、异常、并发与资源验收',
        'project-plan':'拆解需求、依赖、里程碑与交付物',
        'technical-writing':'形成结构清晰、区分事实与建议的技术说明',
        'web-research':'检索公开网页并核对来源与时效',
        'workspace-engineering':'在隔离项目中修改文件、运行命令并验证',
        'data-validation':'检查JSON、CSV和计算的输入与结论',
        'runtime-operations':'先观测资源与失败，再有限调整Runtime配置',
        'knowledge-analysis':'基于命中资料回答、交叉检查并引用可核对的来源',
        'engineering-repair':'用最少模型生成完成原始失败、补丁、测试和同版本交付',
        'source-research':'使用本地资料与官方来源核对事实',
        'sql-review':'检查SQLite参数化、事务、NULL和数据契约'}
PLUGINS={'text.stats':'统计文本字符、行与词数',
         'csv.aggregate':'对CSV指定列做精确十进制求和，不调用模型',
         'computer.run':'在独立实验桌面进行限步浏览、截图、点击、输入和滚动，统一模型预算',
         'math.calculate':'计算有限的基础算术表达式',
         'python.syntax':'检查Python语法并提取函数和类名，不执行代码',
         'project.inspect':'读取三个固定示例项目的只读源码',
         'project.test':'实际运行固定示例项目的不可修改测试',
         'project.patch_check':'验证固定项目补丁的AST并运行不可修改测试',
         'text.search':'按字面关键词查找文本行，不执行正则表达式',
         'text.diff':'比较两段文本并返回有限的统一差异',
         'json.validate':'解析并检查JSON结构，不执行内容',
         'csv.profile':'检查CSV行数、列数及缺失值',
         'web.fetch':'读取公网HTTPS文本，阻止内网访问和DNS重绑定',
         'command.run':'在bubblewrap隔离目录运行命令，禁止主机文件和网络访问',
         'file.read':'读取项目目录UTF-8文件，不能访问主机其他路径',
         'file.write':'原子修改项目目录UTF-8文件，不能访问主机其他路径',
         'file.copy':'按字节复制项目内文本文件，保留测试与格式，避免模型重写',
         'file.replace':'按预期哈希和唯一文本片段精确修改项目文件',
         'file.list':'列出项目目录文件',
         'artifact.write':'在项目目录生成可下载的文本产物',
         'runtime.observe':'读取当前工作流资源和Agent状态',
         'runtime.configure':'在当前服务上限内调整工作流并发数或调度策略',
         'context.search':'检索当前Agent已获授权的上下文语义片段、来源与冲突主张',
         'context.read':'按不可变引用分页读取已获授权的完整原始上下文',
         'workspace.inspect':'读取当前Python项目的源码、测试清单与哈希',
         'workspace.import':'把本会话Python附件按原文件名导入工程，拒绝覆盖不同文件',
         'workspace.apply_patch':'校验整批哈希后修改已有源码，禁止修改测试',
         'workspace.test':'隔离运行unittest，拒绝0测试或全部跳过的假成功',
         'library.search':'检索版本化的Python、SQL、Git、HTTP和Linux资料',
         'library.read':'按ID读取带官方来源的本地资料条目',
         'tools.discover':'按名称或关键词读取工具参数与权限',
         'clock.now':'读取实际服务器时间，明确IANA时区与来源',
         'project.apply_edits':'编译受检AST表达式补丁并执行原项目验证'}
ARGUMENTS={
 'computer.run':{'goal':'打开网页、根据观察完成操作并核对','url':'https://agentrt-lab.invalid/','max_steps':8},
 'text.stats':{'text':'可选文本，省略时使用用户请求'},'math.calculate':{'expression':'(12+3)*2'},
 'python.syntax':{'source':'def add(a,b):\n return a+b'},'project.inspect':{'project':'pricing/intervals/text_normalization'},
 'project.test':{'project':'pricing/intervals/text_normalization'},
 'project.patch_check':{'project':'pricing','source':'完整的固定项目补丁模块'},
 'text.search':{'text':'待查找文本','query':'字面关键词'},'text.diff':{'before':'原文','after':'修改稿'},
 'json.validate':{'text':'JSON文本'},'csv.profile':{'text':'CSV文本'},'csv.aggregate':{'text':'name,amount\nA,1.25','column':'amount'},
 'web.fetch':{'url':'https://example.com/'},'command.run':{'argv':['python3','-m','unittest'],'timeout':10},
 'file.read':{'path':'src/main.py','offset':0,'limit':12000},'file.write':{'path':'src/main.py','content':'文件内容'},'file.list':{},
 'file.copy':{'source':'uploads/附件id.py','target':'test_calc.py'},
 'file.replace':{'path':'calc.py','old':'return a - b','new':'return a + b','sha256':'file.read返回的原文件哈希'},
 'artifact.write':{'path':'report.md','content':'产物正文'},'runtime.observe':{},
 'runtime.configure':{'workers':2,'policy':'fifo/resource_fit/aging_fit/drf'},
 'context.search':{'query':'并发预算与上游失败','limit':6},'context.read':{'ref':'64位source_ref','offset':0,'limit':4000},
 'workspace.inspect':{},'workspace.test':{},'workspace.import':{'files':[{'path':'uploads/附件id.py','name':'calc.py'},{'path':'uploads/测试附件id.py','name':'test_calc.py'}]},'workspace.apply_patch':{'patches':[{'path':'calc.py','sha256':'原SHA','content':'完整新源码'}]},
 'library.search':{'query':'Python测试','limit':4},'library.read':{'id':'python-testing'},'tools.discover':{'query':'file.read'},
 'clock.now':{'timezone':'Asia/Shanghai'},'project.apply_edits':{'project':'pricing','edits':[{'site':'列出的位置','expression':'表达式'}]}}


def catalog():
    return {'skills':[{'id':key,'description':value} for key,value in SKILLS.items()],
            'plugins':[{'id':key,'description':value,'arguments_example':ARGUMENTS[key],
                'permissions':['isolated_desktop_and_model_budget' if key=='computer.run' else 'sandboxed_command' if key=='command.run' else 'public_network' if key=='web.fetch'
                    else 'workspace_write' if key in ('file.write','file.copy','file.replace','artifact.write','workspace.apply_patch','workspace.import','project.patch_check','project.apply_edits') else 'sandboxed_command' if key=='workspace.test' else 'runtime_control' if key=='runtime.configure' else 'bounded_read_or_compute']}
                for key,value in PLUGINS.items()],
            'host_arbitrary_code_execution':False,'external_plugins_installed':False}


def load_skill(name):
    if name not in SKILLS:raise ValueError('未注册的Skill')
    path=SKILL_ROOT/(name+'.md')
    if path.is_symlink() or path.resolve().parent!=SKILL_ROOT.resolve():raise ValueError('Skill路径无效')
    raw=path.read_bytes()
    if len(raw)>8192:raise ValueError('Skill超过大小上限')
    return raw.decode('utf-8')


def calculate(expression):
    if not isinstance(expression,str) or not 1<=len(expression)<=256:raise ValueError('表达式大小无效')
    tree=ast.parse(expression,mode='eval')
    if len(list(ast.walk(tree)))>64:raise ValueError('表达式过于复杂')
    operations={ast.Add:lambda a,b:a+b,ast.Sub:lambda a,b:a-b,ast.Mult:lambda a,b:a*b,
        ast.Div:lambda a,b:a/b,ast.FloorDiv:lambda a,b:a//b,ast.Mod:lambda a,b:a%b}
    def visit(node):
        if isinstance(node,ast.Constant) and type(node.value) in (int,float):value=node.value
        elif isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):
            value=visit(node.operand)*(1 if isinstance(node.op,ast.UAdd) else -1)
        elif isinstance(node,ast.BinOp) and type(node.op) in operations:value=operations[type(node.op)](visit(node.left),visit(node.right))
        else:raise ValueError('仅支持基础算术，不支持函数、变量或幂运算')
        if isinstance(value,int) and value.bit_length()>1024:raise ValueError('整数超过限制')
        if isinstance(value,float) and not math.isfinite(value):raise ValueError('数值不有限')
        return value
    return visit(tree.body)


def execute_plugin(task):
    name=task.get('plugin');args=task.get('arguments',{})
    normalizations=[]
    if name=='csv.aggregate':
        from decimal import Decimal,InvalidOperation,localcontext
        if set(args)!={'text','column'} or not isinstance(args['text'],str) or len(args['text'])>20000 or not isinstance(args['column'],str):raise ValueError('需要有限CSV文本与column')
        reader=csv.DictReader(io.StringIO(args['text']))
        if not reader.fieldnames or len(set(reader.fieldnames))!=len(reader.fieldnames) or args['column'] not in reader.fieldnames:raise ValueError('列名不存在或重复')
        with localcontext() as ctx:
            ctx.prec=100;total=Decimal(0);count=0
            for row in reader:
                if None in row:raise ValueError('CSV行存在多余字段')
                raw=row.get(args['column'])
                if not isinstance(raw,str) or not re.fullmatch(r'[+-]?\d{1,30}(?:\.\d{1,20})?',raw.strip()):raise ValueError('求和列必须是有限十进制数，不跳过空值')
                total+=Decimal(raw.strip());count+=1
            return {'plugin':name,'executed':True,'rows':count,'sum_decimal':format(total,'f'),'column':args['column']}
    if name not in PLUGINS or not isinstance(args,dict):raise ValueError('插件或参数无效')
    if name=='computer.run':
        from .computer import execute
        return execute(task,args)
    if name=='clock.now':
        from .clock_tool import now
        if set(args)-{'timezone'}:raise ValueError('时间工具参数无效')
        return {'plugin':name,**now(args.get('timezone','Asia/Shanghai'))}
    if name=='project.apply_edits':
        from .compact_patch import compile_patch
        from .engineering import canonical_project
        if set(args)!={'project','edits'}:raise ValueError('表达式补丁参数无效')
        project=canonical_project(args['project']);source=compile_patch(json.dumps({'edits':args['edits']}),project)
        return {'edit_protocol':'ast_expressions',**execute_plugin({**task,'plugin':'project.patch_check','arguments':{'project':project,'source':source}})}
    if name in ('file.write','artifact.write') and 'content_from' in args:
        source=args['content_from']
        if (set(args)!={'path','content_from'} or not isinstance(source,str)
                or source not in task.get('inputs',[]) or source not in task.get('_inputs',{})):
            raise ValueError('content_from必须引用当前任务已声明并授权的上游结果，不能同时提供content')
        content=task['_inputs'][source]
        if not isinstance(content,str) or not content.strip():raise ValueError('上游文件正文必须是非空文本')
        if content.strip().startswith('```'):
            lines=content.strip().splitlines()
            if len(lines)<3 or lines[-1]!='```':raise ValueError('上游代码围栏不完整')
            content='\n'.join(lines[1:-1])
        if isinstance(args.get('path'),str) and args['path'].lower().endswith('.svg'):
            from .svg_artifact import normalize_generated_svg
            content,normalizations=normalize_generated_svg(content)
        args={'path':args['path'],'content':content}
    if name.startswith(('file.','web.','workspace.')) or name in ('command.run','artifact.write'):
        from .tooling import operate
        result={'plugin':name,**operate(task,name,args)}
        if normalizations:result['generated_syntax_repairs']=normalizations
        return result
    if name=='tools.discover':
        query=args.get('query')
        if set(args)!={'query'} or not isinstance(query,str) or not 1<=len(query)<=200:raise ValueError('需要有限的query')
        return {'plugin':name,'tools':[row for row in catalog()['plugins'] if query.lower() in (row['id']+' '+row['description']).lower()][:6],'executed':True}
    if name.startswith('library.'):
        from . import library
        if name=='library.read' and set(args)=={'id'}:return {'plugin':name,**library.read(args['id']),'executed':True}
        if name=='library.search' and not set(args)-{'query','limit'} and 'query' in args:return {'plugin':name,'sources':library.search(**args),'executed':True}
        raise ValueError('资料工具参数无效')
    if name.startswith('runtime.'):
        from .worker import ipc
        if name=='runtime.observe':
            if args:raise ValueError('观测工具不接收参数')
            return {'plugin':name,**ipc(task,op='observe'),'executed':True}
        return {'plugin':name,**ipc(task,op='configure',settings=args),'executed':True}
    if name.startswith('context.'):
        from .worker import ipc
        allowed={'query','limit'} if name=='context.search' else {'ref','offset','limit'}
        required={'query'} if name=='context.search' else {'ref'}
        if set(args)-allowed or not required<=set(args):raise ValueError('上下文工具参数无效')
        return {'plugin':name,**ipc(task,op='context_search' if name=='context.search' else 'context_read',**args),'executed':True}
    if name in ('text.search','text.diff','json.validate','csv.profile'):
        expected={'text','query'} if name=='text.search' else {'before','after'} if name=='text.diff' else {'text'}
        if set(args)!=expected or any(not isinstance(v,str) or len(v)>20000 for v in args.values()):raise ValueError('文本工具参数无效')
        if name=='text.search':
            if not args['query'] or len(args['query'])>200:raise ValueError('关键词需为1—200字')
            return {'plugin':name,'matches':[{'line':i,'text':line[:500]} for i,line in enumerate(args['text'].splitlines(),1)
                if args['query'] in line][:100],'executed':True}
        if name=='text.diff':return {'plugin':name,'diff':'\n'.join(difflib.unified_diff(args['before'].splitlines(),args['after'].splitlines()))[:16000],'executed':True}
        if name=='json.validate':
            try:value=json.loads(args['text'],parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite')))
            except (ValueError,RecursionError):return {'plugin':name,'valid':False,'executed':True}
            return {'plugin':name,'valid':True,'type':type(value).__name__,'items':len(value) if isinstance(value,(list,dict)) else None,'executed':True}
        rows=list(csv.reader(io.StringIO(args['text'])));width=len(rows[0]) if rows else 0
        return {'plugin':name,'rows':max(0,len(rows)-1),'columns':width,'ragged_rows':sum(len(r)!=width for r in rows[1:]),
            'missing_cells':sum(not c.strip() for r in rows[1:] for c in r),'executed':True}
    if name in ('project.test','project.patch_check'):
        expected={'project','source'} if name=='project.patch_check' else {'project'}
        if set(args)!=expected:raise ValueError('项目测试参数无效')
        from .engineering import manifest,run_check,canonical_project,validate_patch
        project=canonical_project(args['project']);source=args.get('source',manifest(project)['source'])
        report=run_check(source,Path(task['_workspace'])/'plugin-tests',project=project)
        artifact={}
        import os
        if name=='project.patch_check' and report['success'] and os.name=='posix':
            from .tooling import operate
            normalized=validate_patch({'source':source},project)
            artifact=operate(task,'file.write',{'path':'verified/'+project+'_'+report['source_sha256']+'.py','content':normalized})
            if artifact['sha256']!=report['source_sha256']:raise ValueError('已测源码与产物哈希不一致')
        return {'plugin':name,'project':project,'success':report['success'],'source_sha256':report['source_sha256'],**artifact,
            'public':{k:v for k,v in report['public'].items() if k!='stdout'} if report['public'] else None,
            'heldout':{k:v for k,v in report['heldout'].items() if k!='stdout'} if report['heldout'] else None,
            'executed':True,'tests_modified':False}
    if name=='math.calculate':
        if set(args)!={'expression'}:raise ValueError('需要expression参数')
        return {'plugin':name,'value':calculate(args['expression']),'executed':True}
    if name=='project.inspect':
        if set(args)!={'project'}:raise ValueError('需要project参数')
        sources={'pricing':'pricing.py','intervals':'intervals/intervals.py','text_normalization':'text_normalization/normalizer.py'}
        project=args.get('project')
        # project_workflow keeps the authoritative file names.
        from .engineering import manifest,canonical_project
        project=canonical_project(project)
        source=manifest(project)['source']
        if len(source)>24000:raise ValueError('示例源码超过上限')
        return {'plugin':name,'project':project,'source':source,'read_only':True,'executed':True}
    if set(args)-{'text','source'}:raise ValueError('未知插件参数')
    text=args.get('source' if name=='python.syntax' else 'text')
    if text is None:
        text='\n'.join(str(c.get('user_request','')) for c in task.get('_contexts',[]) if isinstance(c,dict))
    if not isinstance(text,str) or len(text)>20000:raise ValueError('插件输入需为20000字以内文本')
    if name=='text.stats':return {'plugin':name,'characters':len(text),'lines':len(text.splitlines()),
        'words':len(re.findall(r'\w+',text)),'executed':True}
    try:tree=ast.parse(text)
    except SyntaxError as exc:return {'plugin':name,'valid':False,'line':exc.lineno,'executed':True}
    return {'plugin':name,'valid':True,'functions':[n.name for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))],
            'classes':[n.name for n in ast.walk(tree) if isinstance(n,ast.ClassDef)],'executed':True,'code_executed':False}
