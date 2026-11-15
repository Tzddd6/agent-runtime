"""Public-network reads and project-scoped operations; no ambient host access."""
import base64
import difflib
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import time
import uuid
from urllib.parse import urlsplit,urljoin

MAX_FILE=1024*1024


def workspace(task):
    root=Path(task.get('_project_workspace') or task['_workspace']).resolve()
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    return root


def scoped_path(root,name):
    if (not isinstance(name,str) or not 1<=len(name)<=200 or '\\' in name or ':' in name
        or any(p in ('','..','.') or p.startswith('.') for p in name.split('/'))
        or name.startswith('/') or '\x00' in name):raise ValueError('文件路径必须位于项目目录，不能包含隐藏或上级路径')
    target=root/name
    for parent in [target,*target.parents]:
        if parent==root:break
        if parent.is_symlink():raise ValueError('不允许符号链接文件路径')
    if not target.resolve().is_relative_to(root):raise ValueError('文件路径越界')
    if target.suffix.lower() in ('.pem','.key','.exe','.so','.dll'):raise ValueError('不允许凭据或二进制文件')
    return target


def workspace_usage(root):
    count=size=0
    for current,dirs,files,descriptor in os.fwalk(root,follow_symlinks=False):
        count+=len(dirs)+len(files)
        if count>512:raise ValueError('项目目录超过512个文件或目录')
        for name in files:
            try:info=os.stat(name,dir_fd=descriptor,follow_symlinks=False)
            except FileNotFoundError:continue
            if stat.S_ISREG(info.st_mode):size+=info.st_size
        if size>32*1024**2:raise ValueError('项目目录超过32MiB配额')
    return size


def parent_descriptor(root,name,create=False):
    parts=name.split('/');descriptor=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            if create:
                try:os.mkdir(part,0o700,dir_fd=descriptor)
                except FileExistsError:pass
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            os.close(descriptor);descriptor=child
        return descriptor,parts[-1]
    except BaseException:os.close(descriptor);raise


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


def command(task,args):
    argv=args.get('argv');seconds=args.get('timeout',10)
    allowed={'python3','bash','git','ls','cat','grep','sed','find','gcc','make','pwd','wc','diff'}
    if (not isinstance(argv,list) or not argv or len(argv)>32 or any(not isinstance(a,str) or len(a)>8000 or '\x00' in a for a in argv)
        or argv[0] not in allowed or type(seconds) not in (int,float) or not 0<seconds<3600):raise ValueError('命令或超时不在许可范围')
    seconds=min(seconds,20)
    if os.name!='posix' or not shutil.which('bwrap'):raise ValueError('缺少bubblewrap，拒绝降级到主机命令执行')
    root=workspace(task);workspace_usage(root)
    cmd=['bwrap','--die-with-parent','--unshare-all','--clearenv','--ro-bind','/usr','/usr',
        '--symlink','usr/bin','/bin','--symlink','usr/lib','/lib','--symlink','usr/lib64','/lib64',
        '--proc','/proc','--dev','/dev','--tmpfs','/tmp','--dir','/etc',
        '--bind',str(root),'/workspace','--chdir','/workspace','--setenv','PATH','/usr/bin',
        '--setenv','HOME','/tmp','--setenv','LANG','C.UTF-8','--setenv','PYTHONDONTWRITEBYTECODE','1','--', '/usr/bin/python3','-I','-c',
        'import os,resource,sys;resource.setrlimit(resource.RLIMIT_FSIZE,(8388608,8388608));os.execvp(sys.argv[1],sys.argv[1:])',*argv]
    start=time.monotonic();output=bytearray();reason=None
    proc=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
        env={'PATH':'/usr/bin','LANG':'C.UTF-8'},start_new_session=True)
    selector=selectors.DefaultSelector();selector.register(proc.stdout,selectors.EVENT_READ)
    def kill():
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
    try:
        while selector.get_map():
            if time.monotonic()-start>seconds:reason='timeout';break
            try:workspace_usage(root)
            except ValueError:reason='workspace_quota';break
            for key,_ in selector.select(.1):
                block=os.read(key.fileobj.fileno(),4096)
                if not block:selector.unregister(key.fileobj);continue
                output.extend(block)
                if len(output)>65536:reason='output_limit';break
            if reason:break
        if reason and proc.poll() is None:kill()
        try:code=proc.wait(timeout=max(.1,seconds-(time.monotonic()-start)))
        except subprocess.TimeoutExpired:
            reason='timeout';kill();code=proc.wait(timeout=2)
    finally:
        if proc.poll() is None:kill();proc.wait(timeout=2)
        selector.close();proc.stdout.close()
    return {'ok':code==0 and reason is None,'exit_code':code,'stop_reason':reason,
        'output':bytes(output[:65536]).decode('utf-8',errors='replace'),'seconds':time.monotonic()-start,
        'isolation':'bubblewrap_user_mount_pid_network','network_enabled':False,'executed':True}


def operate(task,name,args):
    if (name.startswith('file.') or name in ('command.run','artifact.write')) and not task.get('_workspace_locked'):
        if os.name!='posix':raise ValueError('文件与命令权限工具要求Linux隔离环境')
        import fcntl
        root=workspace(task);locks=root.parent/'.agentrt-locks';locks.mkdir(exist_ok=True,mode=0o700)
        descriptor=os.open(locks/(hashlib.sha256(str(root).encode()).hexdigest()+'.lock'),
            os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(descriptor,fcntl.LOCK_EX)
            return operate({**task,'_workspace_locked':True},name,args)
        finally:os.close(descriptor)
    if name=='web.fetch':
        if set(args)!={'url'}:raise ValueError('需要url参数')
        return fetch_public(args['url'])
    if name=='command.run':
        if set(args)-{'argv','timeout'}:raise ValueError('未知命令参数')
        return command(task,args)
    if name in ('file.copy','file.replace'):
        expected={'source','target'} if name=='file.copy' else {'path','old','new','sha256'}
        if set(args)!=expected:raise ValueError('复制或替换参数无效')
        source=args.get('source') if name=='file.copy' else args.get('path')
        full={**task,'_download_full':True}
        original=operate(full,'file.read',{'path':source})
        if name=='file.copy':target=args['target'];content=original['content']
        else:
            if (not isinstance(args['old'],str) or not args['old'] or not isinstance(args['new'],str)
                or original['sha256']!=args['sha256'] or original['content'].count(args['old'])!=1):raise ValueError('文件已变化或替换片段不唯一')
            target=source;content=original['content'].replace(args['old'],args['new'],1)
        result=operate(task,'file.write',{'path':target,'content':content})
        result['before_sha256']=original['sha256'];return result
    if name in ('file.read','file.write','file.list','artifact.write'):
        root=workspace(task);workspace_usage(root)
        if name=='file.list':
            if args:raise ValueError('file.list不接收参数')
            return {'files':[str(p.relative_to(root)) for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink()][:256],'executed':True}
        allowed={'path','offset','limit'} if name=='file.read' else {'path','content'}
        if set(args)-allowed:raise ValueError('未知文件参数')
        target=scoped_path(root,args.get('path'))
        if name=='file.read':
            directory,leaf=parent_descriptor(root,args['path'])
            try:
                descriptor=os.open(leaf,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
                with os.fdopen(descriptor,'rb') as f:
                    if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):raise ValueError('只能读取普通文件')
                    raw=f.read(MAX_FILE+1)
            finally:os.close(directory)
            if len(raw)>MAX_FILE:raise ValueError('文件超过1MiB读取上限')
            text=raw.decode('utf-8');offset=args.get('offset',0);limit=args.get('limit',12000)
            if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=20000:raise ValueError('读取范围无效')
            if task.get('_download_full'):offset=0;limit=len(text)
            return {'path':args['path'],'content':text[offset:offset+limit],'truncated':offset+limit<len(text),
                'sha256':hashlib.sha256(raw).hexdigest(),'executed':True}
        content=args.get('content')
        if not isinstance(content,str) or len(content.encode())>MAX_FILE:raise ValueError('写入内容超过1MiB上限')
        if re.search(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----',content):raise ValueError('拒绝生成或保存私钥内容')
        raw=content.encode();used=workspace_usage(root)
        if used+len(raw)>32*1024**2:raise ValueError('项目目录配额不足')
        directory,leaf=parent_descriptor(root,args['path'],create=True);temporary='.agentrt-'+uuid.uuid4().hex
        try:
            descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
            with os.fdopen(descriptor,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
            os.replace(temporary,leaf,src_dir_fd=directory,dst_dir_fd=directory)
            # Timestamp+size bytecode validation can reuse stale code after a
            # same-size atomic edit within one second. Invalidate scoped caches.
            if leaf.endswith('.py'):
                try:cache=os.open('__pycache__',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
                except FileNotFoundError:cache=None
                if cache is not None:
                    try:
                        for item in os.listdir(cache):
                            if item.startswith(leaf[:-3]+'.') and item.endswith('.pyc'):
                                try:os.unlink(item,dir_fd=cache)
                                except FileNotFoundError:pass
                    finally:os.close(cache)
        finally:
            try:os.unlink(temporary,dir_fd=directory)
            except FileNotFoundError:pass
            os.close(directory)
        return {'path':args['path'],'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'written':True,'executed':True}
    raise ValueError('未知系统工具')
