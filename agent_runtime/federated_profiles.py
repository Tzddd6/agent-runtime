"""Upgrade profile routing without restarting or extracting old RAM owners."""
import copy,json,os,re,stat
from pathlib import Path
from .model_profiles import MultiModelBroker
from .model_gateway import ModelError

class FederatedProfiles(MultiModelBroker):
    def __init__(self,token,*,upstream,selection_path=None,**kwargs):
        super().__init__(token,**kwargs);self.upstream=upstream
        self.selected=upstream.status().get('profile_id') or 'legacy'
        self.selection_path=Path(selection_path) if selection_path else None
        if self.selection_path and self.selection_path.exists():
            info=self.selection_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077 or info.st_size>128:raise ModelError('Unsafe model selection metadata')
            value=json.loads(self.selection_path.read_text())
            identity=value.get('selected') if isinstance(value,dict) and set(value)=={'selected'} else None
            self.selected=identity if any(p['id']==identity and p['configured'] for p in self.external_profiles()) else None

    def persist_selection(self,identity):
        if self.selection_path is None:return
        if identity is not None and (not isinstance(identity,str) or not re.fullmatch('[a-z][a-z0-9_-]{0,31}',identity)):raise ValueError('Invalid selection metadata')
        parent=self.selection_path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.geteuid() or stat.S_IMODE(parent.st_mode)&0o077:raise ModelError('Unsafe model selection directory')
        import tempfile
        fd,name=tempfile.mkstemp(prefix='.selection-',dir=self.selection_path.parent)
        try:
            with os.fdopen(fd,'w') as f:json.dump({'selected':identity},f);f.flush();os.fsync(f.fileno())
            os.replace(name,self.selection_path)
        finally:Path(name).unlink(missing_ok=True)

    def configure(self,key):
        result=super().configure(key)
        self.persist_selection(self.selected)
        return result

    def external_profiles(self):return self.upstream.profiles_info()['profiles']

    def profiles_info(self):
        with self.lock:
            external=[dict(p,upstream=True) for p in self.external_profiles()]
            local=[dict(p,upstream=False) for p in super().profiles_info()['profiles'] if not p['legacy']]
            if {p['id'] for p in external}&{p['id'] for p in local}:raise ModelError('API profile identity collision')
            return {'selected':self.selected,'profiles':external+local,'max_profiles':8,'key_storage':'memory','automatic_failover':False}

    def idle(self):
        super().idle()
        if self.upstream.status().get('busy'):raise ModelError('Existing model calls prevent configuration changes')

    def save_profile(self,value):
        with self.lock:
            self.idle();external=self.external_profiles()
            if isinstance(value,dict) and value.get('id') in {p['id'] for p in external}:
                return self.upstream.save_profile(value)
            if isinstance(value,dict) and value.get('id') not in self.profiles and len(external)+len(self.profiles)>=8:raise ValueError('At most eight API profiles')
            return super().save_profile(value)

    def select_profile(self,value):
        if not isinstance(value,dict) or set(value)!={'id'} or not isinstance(value['id'],str):raise ValueError('Invalid profile selection')
        with self.lock:
            self.idle();row=next((p for p in self.profiles_info()['profiles'] if p['id']==value['id']),None)
            if row is None or not row['configured']:raise ValueError('Select a configured API profile')
            self.persist_selection(row['id']);self.selected=row['id'];return self.status()

    def remove_profile(self,value):
        if not isinstance(value,dict) or set(value)!={'id'} or value['id']=='legacy':raise ValueError('Invalid profile removal')
        with self.lock:
            self.idle()
            if value['id'] in {p['id'] for p in self.external_profiles()}:
                result=self.upstream.remove_profile(value)
                if self.selected==value['id']:self.persist_selection(None);self.selected=None
                return {'removed':bool(result['removed']),'selected':self.selected}
            result=super().remove_profile(value)
            self.persist_selection(self.selected)
            return result

    def snapshot_client(self,value):
        identity=value.get('profile_id',self.selected)
        if identity!='legacy':
            row=next((p for p in self.external_profiles() if p['id']==identity),None)
            if row:
                if value.get('profile_revision',row['revision'])!=row['revision']:raise ModelError('API profile changed before admission')
                client=self.upstream.for_profile(identity,row['model'])
                if client.profile_revision!=row['revision']:raise ModelError('API profile changed during admission')
                return lambda:copy.copy(client),{'profile_id':identity,'profile_revision':row['revision'],'provider':row['provider']}
        return super().snapshot_client(value)
