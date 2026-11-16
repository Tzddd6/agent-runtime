"""Model-authored AST expression edits, compiled through the original validator.

No expected solutions or hidden-test answers live here. Only listed AST sites
may change, and the complete reconstructed module passes the same safety gate.
"""
import ast
import hashlib
import json
from .engineering import manifest,validate_patch,_json_object,_non_json_constant


def sites_for(source):
    tree=ast.parse(source);sites={}
    for function in tree.body:
        counters={}
        for node in ast.walk(function):
            field='value' if isinstance(node,(ast.Return,ast.Assign)) else 'test' if isinstance(node,ast.If) else None
            if field is None or getattr(node,field,None) is None:continue
            kind='return' if isinstance(node,ast.Return) else 'assign' if isinstance(node,ast.Assign) else 'if'
            index=counters.get(kind,0);counters[kind]=index+1
            key=function.name+'.'+kind+'.'+str(index)
            sites[key]=(node,field)
    return tree,sites


def describe(project):
    spec=manifest(project);_,sites=sites_for(spec['source'])
    tree=ast.parse(spec['source'])
    return {'project_id':project,'requirements':spec['requirements'],'public_examples':spec['public_examples'],
            'signatures':[f.name+'('+','.join(a.arg for a in f.args.args)+')' for f in tree.body],
            'edit_sites':[{'site':key,'expression':ast.unparse(getattr(node,field))} for key,(node,field) in sites.items()],
            'base_sha256':hashlib.sha256(spec['source'].encode()).hexdigest()}


def compile_patch(raw,project):
    if not isinstance(raw,str) or len(raw.encode())>16000:raise ValueError('表达式补丁大小无效')
    value=json.loads(raw,object_pairs_hook=_json_object,parse_constant=_non_json_constant)
    if not isinstance(value,dict) or set(value)!={'edits'} or not isinstance(value['edits'],list) or not 1<=len(value['edits'])<=12:raise ValueError('只接收1—12项edits')
    tree,sites=sites_for(manifest(project)['source']);used=set()
    for edit in value['edits']:
        if not isinstance(edit,dict) or set(edit)!={'site','expression'} or not isinstance(edit['site'],str) or edit['site'] not in sites or edit['site'] in used:raise ValueError('表达式位置无效或重复')
        text=edit['expression']
        if not isinstance(text,str) or not 1<=len(text)<=2000:raise ValueError('表达式大小无效')
        expression=ast.parse(text,mode='eval').body
        if len(list(ast.walk(expression)))>128:raise ValueError('表达式过于复杂')
        node,field=sites[edit['site']];setattr(node,field,expression);used.add(edit['site'])
    ast.fix_missing_locations(tree)
    return validate_patch({'source':ast.unparse(tree)},project)
