"""Bounded, RAM-only provider profiles. No supplier key is returned or logged.

The optional legacy client delegates to the original credential owner, so adding
OpenAI does not require restarting it or reading/transferring its existing key.
"""
import re
from .model_broker import ModelBroker,ACTIVE
from .model_gateway import DeepSeekClient,OpenAIClient,ModelError


class MultiModelBroker(ModelBroker):
    def __init__(self,token,*,legacy_client=None,profile_factory=None,**kwargs):
        super().__init__(token,**kwargs)
        self.legacy_client=legacy_client;self.profiles={};self.selected='legacy'
        self.profile_factory=profile_factory or self.build_client

    @staticmethod
    def build_client(provider,key,model):
        return (OpenAIClient if provider=='openai' else DeepSeekClient)(key,model=model)

    def idle(self):
        if self.closed or any(c['status'] in ACTIVE for c in self.calls.values()):
            raise ModelError('Wait for model calls to finish before changing profiles')

    @staticmethod
    def key_valid(key):
        if not isinstance(key,str) or not 16<=len(key)<=512 or any(c.isspace() for c in key):
            raise ValueError('Invalid supplier key format')

    def legacy_status(self):
        if self.legacy_client is None:return {'configured':bool(self._key),'model':'deepseek-flash'}
        try:return self.legacy_client.status()
        except ModelError:return {'configured':False,'model':'deepseek-flash','available':False}

    def profiles_info(self):
        with self.lock:
            legacy=self.legacy_status()
            rows=[{'id':'legacy','label':'DeepSeek · 原有密钥服务','provider':'deepseek','model':legacy['model'],'configured':bool(legacy['configured']),'legacy':True,'revision':None}]
            rows.extend({k:p[k] for k in ('id','label','provider','model','revision')}|{'configured':bool(p['key']),'legacy':False} for p in self.profiles.values())
            return {'selected':self.selected,'profiles':rows,'max_profiles':8,'key_storage':'memory','automatic_failover':False}

    def status(self):
        with self.lock:
            info=self.profiles_info();selected=next((p for p in info['profiles'] if p['id']==self.selected),None)
            return {'configured':bool(selected and selected['configured']),'model':selected['model'] if selected else '未选择模型',
                'provider':selected['provider'] if selected else None,'profile_id':self.selected,'profile_revision':selected['revision'] if selected else None,'profiles_supported':True,
                'busy':sum(c['status'] in ACTIVE for c in self.calls.values())}

    def configure(self,key):
        if not isinstance(key,str) or len(key)>512 or any(c.isspace() for c in key):raise ValueError('Invalid supplier key format')
        if key:self.key_valid(key)
        with self.lock:
            self.idle()
            if self.legacy_client is not None:result=self.legacy_client.configure(key)
            else:self._key=key;result={'configured':bool(key)}
            if key:self.selected='legacy'
            return result

    def save_profile(self,value):
        if not isinstance(value,dict) or set(value)-{'id','label','provider','model','key'}:raise ValueError('Invalid profile fields')
        identity=value.get('id');label=value.get('label');provider=value.get('provider');model=value.get('model')
        if (not isinstance(identity,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',identity) or identity=='legacy'
            or not isinstance(label,str) or not 1<=len(label)<=60 or any(ord(c)<32 for c in label)
            or provider not in ('deepseek','openai') or not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}',model)
            or 'sk-' in label.lower() or 'sk-' in model.lower()):raise ValueError('Invalid profile metadata')
        with self.lock:
            self.idle();old=self.profiles.get(identity)
            if old and old['provider']!=provider:raise ValueError('Provider is immutable; create a separate profile with its own key')
            if old is None and len(self.profiles)>=7:raise ValueError('At most eight API profiles including legacy')
            key=value.get('key',old['key'] if old else None);self.key_valid(key)
            if any(secret in label or secret in model for secret in [key,self.token]+[p['key'] for p in self.profiles.values()]):
                raise ValueError('Profile metadata cannot contain credentials')
            revision=(old['revision']+int(any(old[k]!=v for k,v in {'label':label,'provider':provider,'model':model,'key':key}.items()))) if old else 1
            self.profiles[identity]={'id':identity,'label':label,'provider':provider,'model':model,'key':key,'revision':revision}
            # Saving never selects a new vendor or sends a test request.
            return {'id':identity,'configured':True,'selected':self.selected==identity}

    def select_profile(self,value):
        if not isinstance(value,dict) or set(value)!={'id'}:raise ValueError('Invalid profile selection')
        with self.lock:
            self.idle();identity=value['id']
            if identity=='legacy':ready=self.legacy_status()['configured']
            else:ready=isinstance(identity,str) and identity in self.profiles
            if not ready:raise ValueError('Select a configured API profile')
            self.selected=identity
            return self.status()

    def remove_profile(self,value):
        if not isinstance(value,dict) or set(value)!={'id'} or value['id']=='legacy':raise ValueError('Invalid profile removal')
        with self.lock:
            self.idle();identity=value['id']
            if not isinstance(identity,str) or identity not in self.profiles:raise ValueError('Unknown profile')
            self.profiles.pop(identity)
            if self.selected==identity:self.selected=None
            return {'removed':True,'selected':self.selected}

    def snapshot_client(self,value):
        identity=value.get('profile_id',self.selected)
        if identity=='legacy':
            if not self.legacy_status()['configured']:raise ModelError('Legacy provider key is not configured')
            if self.legacy_client is not None:
                from copy import copy
                return lambda:copy(self.legacy_client),{'profile_id':'legacy','provider':'deepseek'}
            factory,key=self.factory,self._key
            return lambda:factory(key),{'profile_id':'legacy','provider':'deepseek'}
        profile=self.profiles.get(identity)
        if profile is None:raise ModelError('Selected API profile is unavailable')
        if value.get('profile_revision',profile['revision'])!=profile['revision']:raise ModelError('API profile changed; submit a new workflow after reviewing configuration')
        provider,key,model=profile['provider'],profile['key'],profile['model'];factory=self.profile_factory
        return lambda:factory(provider,key,model),{'profile_id':identity,'profile_revision':profile['revision'],'provider':provider}

    def close(self):
        super().close()
        with self.lock:self.profiles.clear();self.selected=None
