"""Opt-in expiring transport phase evidence; never accepts text or credentials."""
import json,os,stat,time,uuid
from pathlib import Path
MARKER=Path('/home/tzd/.config/agentrt/transport-diagnostics.json')
LOG=Path('/home/tzd/.config/agentrt/transport-phases.jsonl')
STAGES={'request_start','headers','first_wire','first_text','terminal','decoded','http_error','transport_error','parser_error','callback_rejected','provider_error','missing_terminal'}
FIELDS={'status','sse','wire_bytes','text_bytes','events','callbacks','error_kind'}
class Trace:
 def __init__(self,*,marker=MARKER,log=LOG):
  self.started=time.monotonic();self.identity=uuid.uuid4().hex;self.log=Path(log);self.enabled=False;self.phases={}
  try:
   info=Path(marker).lstat();parent=Path(marker).parent.lstat();now=time.time()
   if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077 or info.st_size>128:return
   if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.geteuid() or stat.S_IMODE(parent.st_mode)&0o077:return
   value=json.loads(Path(marker).read_text());expiry=value.get('expires_at')
   self.enabled=set(value)=={'expires_at'} and type(expiry) in (int,float) and now<expiry<=now+1800
  except (OSError,ValueError,TypeError,AttributeError):pass
 def emit(self,stage,**values):
  if stage not in STAGES or set(values)-FIELDS or any(type(v) not in (int,float,bool) for v in values.values()):raise ValueError('Transport diagnostics allow numbers only')
  elapsed=time.monotonic()-self.started;self.phases[stage]=elapsed
  if not self.enabled:return
  try:
   fd=os.open(self.log,os.O_WRONLY|os.O_APPEND|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
   try:
    info=os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077 or info.st_size>256*1024:return
    os.write(fd,(json.dumps({'call':self.identity,'stage':stage,'seconds':elapsed,**values})+'\n').encode())
   finally:os.close(fd)
  except OSError:pass
