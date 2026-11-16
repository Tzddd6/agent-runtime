"""Bounded arithmetic mutations, accepted only by the existing protected checker.

No project identifiers, variable names, numeric answers or reference solutions.
Moving integer rounding changes semantics: these are hypotheses, never proof.
"""
import ast
import copy
import hashlib


def rounding_candidates(source,functions,limit=4):
    if not isinstance(source,str) or len(source.encode())>40000 or not isinstance(functions,set):
        raise ValueError('本地候选输入无效')
    if type(limit) is not int or not 1<=limit<=4:raise ValueError('本地候选最多4项')
    tree=ast.parse(source);result=[];seen=set()
    for function in tree.body:
        if not isinstance(function,ast.FunctionDef) or function.name not in functions:continue
        for index,node in enumerate(ast.walk(function)):
            replacement=None
            # a - floor(b/c) -> floor((a*c-b)/c)
            if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Sub) and isinstance(node.right,ast.BinOp) and isinstance(node.right.op,ast.FloorDiv):
                a,b,c=node.left,node.right.left,node.right.right
                replacement=ast.BinOp(ast.BinOp(ast.BinOp(copy.deepcopy(a),ast.Mult(),copy.deepcopy(c)),ast.Sub(),copy.deepcopy(b)),ast.FloorDiv(),copy.deepcopy(c))
            # floor(a/b) * c -> floor((a*c)/b), and the symmetric product.
            elif isinstance(node,ast.BinOp) and isinstance(node.op,ast.Mult):
                division=node.left if isinstance(node.left,ast.BinOp) and isinstance(node.left.op,ast.FloorDiv) else node.right if isinstance(node.right,ast.BinOp) and isinstance(node.right.op,ast.FloorDiv) else None
                if division is not None:
                    other=node.right if division is node.left else node.left
                    replacement=ast.BinOp(ast.BinOp(copy.deepcopy(division.left),ast.Mult(),copy.deepcopy(other)),ast.FloorDiv(),copy.deepcopy(division.right))
            if replacement is None:continue
            candidate=copy.deepcopy(tree)
            target=next(f for f in candidate.body if isinstance(f,ast.FunctionDef) and f.name==function.name)
            old=list(ast.walk(target))[index]
            class Replace(ast.NodeTransformer):
                def visit(self,value):
                    if value is old:return ast.copy_location(copy.deepcopy(replacement),value)
                    return super().visit(value)
            candidate=Replace().visit(candidate);ast.fix_missing_locations(candidate)
            text=ast.unparse(candidate);key=hashlib.sha256(text.encode()).hexdigest()
            if key not in seen:
                seen.add(key);result.append({'source':text,'rule':'defer_integer_rounding','function':function.name,'sha256':key})
                if len(result)>=limit:return result
    return result
