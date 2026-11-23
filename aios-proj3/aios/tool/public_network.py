"""Public HTTPS reads with pinned DNS, bounded redirects and text-only responses."""
from html.parser import HTMLParser
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit, urljoin


class TextOnly(HTMLParser):
    def __init__(self):super().__init__();self.hidden=0;self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'):self.hidden+=1
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript') and self.hidden:self.hidden-=1
    def handle_data(self,data):
        if not self.hidden and data.strip():self.parts.append(data.strip())


def public_address(host):
    addresses=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    if not addresses:raise ValueError('DNS未返回地址')
    for item in addresses:
        if not ipaddress.ip_address(item[4][0]).is_global:raise ValueError('网页工具禁止访问内网、回环或保留地址')
    return addresses[0][4][0]


def fetch_public(url):
    if not isinstance(url,str) or len(url)>2048:raise ValueError('URL大小无效')
    for _ in range(4):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):
            raise ValueError('只允许无凭据的公网HTTPS标准端口')
        host=parsed.hostname.encode('idna').decode();address=public_address(host)
        class PinnedHTTPS(http.client.HTTPSConnection):
            def connect(self):
                sock=socket.create_connection((address,443),timeout=8)
                try:self.sock=ssl.create_default_context().wrap_socket(sock,server_hostname=host)
                except BaseException:sock.close();raise
        connection=PinnedHTTPS(host,timeout=8)
        try:
            connection.request('GET',(parsed.path or '/')+('?' + parsed.query if parsed.query else ''),headers={
                'User-Agent':'AgentRuntime/0.13 (+bounded-public-reader)','Accept':'text/html,text/plain,application/json','Accept-Encoding':'identity'})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                location=response.getheader('Location')
                if not location:raise ValueError('重定向缺少地址')
                url=urljoin(url,location);continue
            kind=response.getheader('Content-Type','').split(';',1)[0].strip().lower()
            if kind not in ('text/html','text/plain','application/json','application/xml','text/xml'):raise ValueError('网页类型不在文本白名单')
            if response.getheader('Content-Encoding','identity').lower() not in ('','identity'):raise ValueError('网页压缩编码不受支持')
            raw=response.read(262145);limited=len(raw)>262144;raw=raw[:262144]
            encoding=response.headers.get_content_charset() or 'utf-8'
            if encoding.lower() not in ('utf-8','utf8','gb18030','gbk','iso-8859-1'):encoding='utf-8'
            text=raw.decode(encoding,errors='replace')
            if kind=='text/html':parser=TextOnly();parser.feed(text);text='\n'.join(parser.parts)
            return {'url':url,'http_status':response.status,'content_type':kind,'text':text[:16000],
                'truncated':limited or len(text)>16000,'source_untrusted':True,'executed':True}
        finally:connection.close()
    raise ValueError('重定向超过3次')

