"""Parse untrusted documents without home mounts, credentials or network access."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile


def extract_document(raw, suffix):
    if os.name != 'posix' or not shutil.which('bwrap'):
        raise ValueError('文档解析需要 Linux 隔离组件；服务尚未就绪')
    needed = {'.pdf':'pdftotext','.doc':'antiword'}.get(suffix)
    if needed and not shutil.which(needed):
        if suffix == '.doc':raise ValueError('此服务暂不支持旧版 DOC，请在 Word 中另存为 DOCX 后上传')
        raise ValueError('服务尚未安装 '+needed+' 文档解析组件')
    parser = Path(__file__).with_name('document_parser.py').resolve()
    with tempfile.TemporaryDirectory(prefix='agentrt-document-') as directory:
        source = Path(directory)/('input'+suffix)
        source.write_bytes(raw);source.chmod(0o600)
        command = ['bwrap','--die-with-parent','--unshare-all','--clearenv',
            '--ro-bind','/usr','/usr','--symlink','usr/bin','/bin','--symlink','usr/lib','/lib',
            '--symlink','usr/lib64','/lib64','--proc','/proc','--dev','/dev','--tmpfs','/tmp',
            '--dir','/etc','--ro-bind',str(parser),'/parser.py','--ro-bind',str(source),'/input',
            '--setenv','PATH','/usr/bin','--setenv','LANG','C.UTF-8',
            '--setenv','HOME','/tmp','--setenv','PYTHONDONTWRITEBYTECODE','1']
        # antiword's packaged mapping files live outside /usr on some distros.
        if suffix == '.doc' and Path('/etc/antiword').is_dir():
            command += ['--ro-bind','/etc/antiword','/etc/antiword']
        command += ['--','/usr/bin/python3','-I','/parser.py','/input',suffix]
        process = subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,env={'PATH':'/usr/bin','LANG':'C.UTF-8'},start_new_session=True)
        try:
            data,_ = process.communicate(timeout=14)
        except subprocess.TimeoutExpired:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.communicate()
            raise ValueError('文档解析超时，请拆分文件后重试') from None
        if len(data)>1024**2:
            raise ValueError('文档解析结果超过上限')
        try:result=json.loads(data)
        except (ValueError,UnicodeError):
            raise ValueError('文档解析服务不可用，请联系管理员检查隔离组件') from None
        if 'error' in result:raise ValueError(result['error'])
        if process.returncode or not isinstance(result.get('text'),str):
            raise ValueError('文档解析失败')
        return result
