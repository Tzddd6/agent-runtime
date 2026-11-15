"""Validated private PNG/JPEG/text attachments. Images normalize before provider use."""
import base64
import hashlib
import io
import os
from pathlib import Path
import re
import time
import uuid
import warnings
from .chat_store import identity

TEXT_SUFFIXES={'.txt','.md','.py','.json','.csv','.log'}


class AttachmentStore:
    def __init__(self,root,database):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700);self.database=database

    def upload(self,value):
        conversation=identity(value.get('conversation_id'));name=value.get('name');encoded=value.get('data')
        if (not isinstance(name,str) or not 1<=len(name)<=120 or '/' in name or '\\' in name or name.startswith('.')
            or any(ord(c)<32 for c in name)
            or not isinstance(encoded,str) or len(encoded)>12*1024**2):raise ValueError('附件名称或大小无效')
        try:raw=base64.b64decode(encoded,validate=True)
        except ValueError:raise ValueError('附件编码无效') from None
        if not raw or len(raw)>8*1024**2:raise ValueError('附件需为1字节至8MiB')
        suffix=Path(name).suffix.lower();width=height=None
        if suffix in ('.png','.jpg','.jpeg'):
            try:
                from PIL import Image,ImageOps
                with warnings.catch_warnings():
                    warnings.simplefilter('error',Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(raw)) as image:
                        if image.format not in ('PNG','JPEG') or image.width*image.height>16000000:raise ValueError('图片格式或像素超过限制')
                        image.verify()
                    with Image.open(io.BytesIO(raw)) as image:
                        image=ImageOps.exif_transpose(image);image.thumbnail((1280,1280))
                        if image.mode=='RGBA':
                            background=Image.new('RGBA',image.size,'white');image=Image.alpha_composite(background,image).convert('RGB')
                        else:image=image.convert('RGB')
                        width,height=image.size
                        for quality in (80,60,40,25):
                            buffer=io.BytesIO();image.save(buffer,format='JPEG',quality=quality);raw=buffer.getvalue()
                            if len(raw)<=256*1024:break
                        else:raise ValueError('图片压缩后仍超过256KiB限制')
            except ImportError:raise ValueError('服务尚未安装图片解码组件') from None
            except Exception:raise ValueError('图片损坏、过大或格式不受支持') from None
            media='image/jpeg'
        elif suffix in TEXT_SUFFIXES:
            if len(raw)>1024**2:raise ValueError('文本附件超过1MiB')
            try:text=raw.decode('utf-8')
            except UnicodeError:raise ValueError('文本附件需为UTF-8') from None
            if '\x00' in text or re.search(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----',text):raise ValueError('拒绝二进制或私钥附件')
            media='text/plain'
        else:raise ValueError('仅支持PNG/JPEG和UTF-8文本、代码、JSON、CSV')
        key=uuid.uuid4().hex;path=self.root/(key+'.bin')
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'wb') as f:f.write(raw)
        row={'id':key,'conversation':conversation,'name':name,'media':media,'size':len(raw),
            'sha256':hashlib.sha256(raw).hexdigest(),'width':width,'height':height,'created':time.time()}
        try:self.database.save_attachment(row)
        except BaseException:path.unlink(missing_ok=True);raise
        return {k:row[k] for k in ('id','name','media','size','width','height')}

    def read(self,key,conversation):
        identity(key);identity(conversation);row=self.database.attachment(key,conversation)
        path=self.root/(key+'.bin')
        if path.is_symlink():raise ValueError('附件不能是符号链接')
        with path.open('rb') as f:raw=f.read(1024**2+1)
        if len(raw)>1024**2 or hashlib.sha256(raw).hexdigest()!=row['sha256']:raise ValueError('附件完整性校验失败')
        return row,raw

    def context(self,keys,conversation,workspace):
        if not isinstance(keys,list) or len(keys)>4 or len(set(keys))!=len(keys):raise ValueError('每次最多4个不同附件')
        metadata=[];images=[]
        from .tooling import operate
        for key in keys:
            row,raw=self.read(key,conversation)
            entry={k:row[k] for k in ('id','name','media','width','height')}
            if row['media'].startswith('image/'):
                if len(images)>=2:raise ValueError('每次最多2张图片')
                images.append({'mime':'image/jpeg','data':base64.b64encode(raw).decode()})
            else:
                file='uploads/'+key+Path(row['name']).suffix.lower()
                operate({'_project_workspace':str(workspace)},'file.write',{'path':file,'content':raw.decode('utf-8')})
                entry.update(path=file,text_excerpt=raw.decode('utf-8')[:4000])
            metadata.append(entry)
        return {'attachments':metadata,'_images':images}
