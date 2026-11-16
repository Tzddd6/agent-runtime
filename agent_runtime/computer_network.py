"""GET-only pinned public HTTPS for a browser with no network namespace access."""
import http.client,socket,ssl,time
from urllib.parse import urlsplit,urljoin
from .tooling import public_address

LAB_URL='https://agentrt-lab.invalid/'
LAB_HTML='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Runtime 实验电脑</title>
<style>body{font:20px sans-serif;max-width:850px;margin:60px auto;background:#f7f9fc;color:#163344}input,button{font:inherit;padding:12px;margin:8px;border-radius:8px}section{background:white;padding:25px;border-radius:16px}#result{padding:18px;background:#ecf7f1}</style>
<h1>Runtime 专用实验桌面</h1><p>合成验收页面，不连接任何真实账户。</p><section><h2>资料查询</h2>
<label for="query">查询编号</label><input id="query" placeholder="输入编号，例如 PINE-442"><button id="search">查询资料</button>
<p id="result" role="status">等待查询</p><h2>工作便签</h2><label for="note">便签内容</label><textarea id="note"></textarea><button id="save">保存便签</button><p id="saved" role="status"></p></section>
<script>document.getElementById('search').onclick=()=>{const q=document.getElementById('query').value.trim();document.getElementById('result').textContent=q==='PINE-442'?'PINE-442：预算95元，负责人Cedar。':'未找到 '+q};document.getElementById('save').onclick=()=>{document.getElementById('saved').textContent='便签已保存：'+document.getElementById('note').value};</script></html>'''

def resource(url,limit=2*1024**2):
    if url==LAB_URL:return {'url':url,'status':200,'type':'text/html; charset=utf-8','body':LAB_HTML.encode(),'fixture':True}
    if not isinstance(url,str) or len(url)>2048:raise ValueError('URL大小无效')
    for _ in range(4):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):raise ValueError('仅允许无凭据的公网HTTPS')
        host=parsed.hostname.encode('idna').decode();address=public_address(host)
        class Pinned(http.client.HTTPSConnection):
            def connect(self):
                sock=socket.create_connection((address,443),timeout=5)
                try:self.sock=ssl.create_default_context().wrap_socket(sock,server_hostname=host)
                except BaseException:sock.close();raise
        connection=Pinned(host,timeout=5)
        try:
            connection.request('GET',(parsed.path or '/')+('?' + parsed.query if parsed.query else ''),headers={'User-Agent':'Mozilla/5.0 AgentRuntimeComputerLab/0.19','Accept':'*/*','Accept-Encoding':'identity'})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                location=response.getheader('Location')
                if not location:raise ValueError('重定向缺少URL')
                url=urljoin(url,location);continue
            if response.getheader('Content-Encoding','identity').lower() not in ('','identity'):raise ValueError('拒绝未受控的压缩资源')
            kind=response.getheader('Content-Type','application/octet-stream')
            allowed=('text/','application/javascript','application/x-javascript','application/json','application/xml','image/','font/','application/font','application/wasm')
            if not kind.lower().startswith(allowed):raise ValueError('浏览器资源类型不在白名单')
            raw=response.read(limit+1)
            if len(raw)>limit:raise ValueError('浏览器单资源超过2MiB')
            return {'url':url,'status':response.status,'type':kind,'body':raw,'fixture':False}
        finally:connection.close()
    raise ValueError('浏览器重定向超过上限')
