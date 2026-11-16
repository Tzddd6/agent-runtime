"""Optional result contracts checked before publishing a task's shared output."""
import json
import math

NAMES=('json_object','semantic_facts')


def validate_result(name,value):
    if name is None:return
    if name not in NAMES:raise ValueError('未知结果契约')
    try:data=json.loads(value) if isinstance(value,str) else value
    except ValueError:raise ValueError('结果契约要求合法JSON对象') from None
    if not isinstance(data,dict):raise ValueError('结果契约要求JSON对象')
    if name=='semantic_facts':
        facts=data.get('semantic_facts')
        if not isinstance(facts,list) or not 1<=len(facts)<=16:raise ValueError('语义事实契约要求1—16条主张；空数组不能表示已完成')
        for fact in facts:
            if (not isinstance(fact,dict) or set(fact)!={'subject','predicate','value'}
                    or any(not isinstance(fact[k],str) or not 1<=len(fact[k])<=100 or any(ord(c)<32 for c in fact[k]) for k in ('subject','predicate'))):
                raise ValueError('语义主张需要subject、predicate、value')
            value=fact['value']
            if value is not None and type(value) not in (str,int,float,bool):raise ValueError('语义主张值需为JSON标量')
            if isinstance(value,str) and len(value)>700 or type(value) is float and not math.isfinite(value):raise ValueError('语义主张值无效或过大')
