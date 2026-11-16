"""Structural standalone SVG gate, not a claim of artistic/semantic correctness."""
import re
import xml.etree.ElementTree as ET


def validate_svg(text):
    if not isinstance(text,str) or len(text.encode('utf-8'))>1024**2:
        raise ValueError('SVG超过1MiB上限')
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():raise ValueError('SVG不允许DTD或实体声明')
    try:root=ET.fromstring(text)
    except (ET.ParseError,ValueError):raise ValueError('SVG不是完整XML，拒绝把占位或截断文本作为图形保存') from None
    if root.tag!='{http://www.w3.org/2000/svg}svg':raise ValueError('SVG需要正确的独立svg根元素和命名空间')
    nodes=list(root.iter())
    if len(nodes)>6000:raise ValueError('SVG元素超过6000个')
    ids={};edges={}
    for node in nodes:
        tag=node.tag.rsplit('}',1)[-1]
        if tag in ('script','foreignObject','iframe','object','embed'):raise ValueError('SVG不允许脚本或嵌入外部页面')
        identity=node.get('id')
        if identity:
            if identity in ids:raise ValueError('SVG元素id重复')
            ids[identity]=node
        for key,value in node.attrib.items():
            local=key.rsplit('}',1)[-1].lower()
            if local.startswith('on') or 'javascript:' in value.lower():raise ValueError('SVG不允许事件处理或脚本链接')
            if local=='href' and not value.startswith('#'):raise ValueError('独立SVG禁止外部引用')
            if any(not target.strip().strip('\"\x27').startswith('#') for target in re.findall(r'url\(([^)]*)\)',value,re.I)):
                raise ValueError('独立SVG禁止外部样式资源')
        if tag=='style':
            style=node.text or ''
            if re.search(r'@import|javascript:',style,re.I) or any(not target.strip().strip('\"\x27').startswith('#') for target in re.findall(r'url\(([^)]*)\)',style,re.I)):
                raise ValueError('SVG样式不得加载外部资源')
    # Reject recursive <use> graphs, including recursion through containing groups.
    for identity,node in ids.items():
        refs=set()
        for child in node.iter():
            if child.tag.rsplit('}',1)[-1]=='use':
                ref=child.get('href') or child.get('{http://www.w3.org/1999/xlink}href','')
                if ref:refs.add(ref[1:])
        edges[identity]=refs
    visiting=set();visited=set()
    def visit(key,depth=0):
        if depth>64 or key in visiting:raise ValueError('SVG use引用形成循环或嵌套过深')
        if key in visited:return
        if key not in ids:raise ValueError('SVG use引用不存在的元素')
        visiting.add(key)
        for target in edges[key]:visit(target,depth+1)
        visiting.remove(key);visited.add(key)
    for key in ids:visit(key)
    return {'xml_valid':True,'standalone':True,'elements':len(nodes),'semantic_constraints_verified':False}
