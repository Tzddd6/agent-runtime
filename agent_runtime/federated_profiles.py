"""Upgrade profile routing without restarting or extracting old RAM owners."""
import copy
from .model_profiles import MultiModelBroker
from .model_gateway import ModelError

class FederatedProfiles(MultiModelBroker):
    def __init__(self,token,*,upstream,**kwargs):
        super().__init__(token,**kwargs);self.upstream=upstream
        self.selected=upstream.status().get('profile_id') or 'legacy'

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
            self.selected=row['id'];return self.status()

    def remove_profile(self,value):
        if not isinstance(value,dict) or set(value)!={'id'} or value['id']=='legacy':raise ValueError('Invalid profile removal')
        with self.lock:
            self.idle()
            if value['id'] in {p['id'] for p in self.external_profiles()}:
                result=self.upstream.remove_profile(value)
                if self.selected==value['id']:self.selected=None
                return {'removed':bool(result['removed']),'selected':self.selected}
            return super().remove_profile(value)

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
