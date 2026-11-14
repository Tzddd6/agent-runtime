"""Conservative workflow-local model admission. No monetary/billing guarantees."""
import threading


DEFAULT_AGENT_MODEL_BUDGET={'calls':8,'output_tokens':8192}
DEFAULT_WORKFLOW_MODEL_BUDGET={'calls':32,'output_tokens':32768}


class ModelBudgetExceeded(RuntimeError):
    pass


def validate_budget(value,default):
    if not isinstance(value,dict) or set(value)-set(default):
        raise ValueError('Model budget must contain calls/output_tokens only')
    result={**default,**value}
    for field,ceiling in (('calls',100),('output_tokens',409600)):
        if type(result[field]) is not int or not 0<=result[field]<=ceiling:
            raise ValueError('Model budget must contain bounded nonnegative integers')
    return result


class ModelBudgetLedger:
    def __init__(self,budget=None):
        self.lock=threading.Lock()
        self.budget=validate_budget({} if budget is None else budget,DEFAULT_WORKFLOW_MODEL_BUDGET)
        self.used={'calls':0,'output_tokens':0}
        self.agents={}

    def register(self,agent,budget=None):
        limits=validate_budget({} if budget is None else budget,DEFAULT_AGENT_MODEL_BUDGET)
        with self.lock:
            if agent in self.agents:
                if self.agents[agent]['budget']!=limits:raise ValueError('Cannot reset Agent model budget')
                return
            self.agents[agent]={'budget':limits,'used':{'calls':0,'output_tokens':0},'rejected':0}

    def admit(self,agent,max_tokens):
        if type(max_tokens) is not int or not 1<=max_tokens<=4096:
            raise ValueError('max_tokens must be between 1 and 4096')
        request={'calls':1,'output_tokens':max_tokens}
        with self.lock:
            entry=self.agents.get(agent)
            if entry is None:raise ModelBudgetExceeded('Agent 未注册模型预算')
            for usage,limits in ((entry['used'],entry['budget']),(self.used,self.budget)):
                if any(usage[k]+request[k]>limits[k] for k in request):
                    entry['rejected']+=1
                    raise ModelBudgetExceeded('模型累计调用或输出额度不足；请求未提交')
            for k in request:
                entry['used'][k]+=request[k];self.used[k]+=request[k]
            # Never refund after provider submission: failed/cancelled requests
            # may still be billed remotely. This is not actual token usage.

    def snapshot(self,agent=None):
        with self.lock:
            e=self.agents[agent] if agent is not None else {'budget':self.budget,'used':self.used}
            return {'budget':dict(e['budget']),'used':dict(e['used']),
                    'remaining':{k:e['budget'][k]-e['used'][k] for k in self.used},
                    'rejected':e.get('rejected',sum(a['rejected'] for a in self.agents.values())),
                    'accounting':'admitted_max_output_tokens_no_refund'}
