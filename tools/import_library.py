"""Explicit, idempotent import of curated notes. Never overwrites user edits."""
import argparse,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_runtime.database import RuntimeDatabase
from agent_runtime.knowledge import KnowledgeStore
from agent_runtime.library import DOCUMENTS,read


def install(store,apply=False):
    rows=[]
    for key in DOCUMENTS:
        note=read(key);identity=hashlib.md5(('agentrt-library-v1:'+key).encode()).hexdigest()
        body=note['text']+'\n\n官方参考（本地整理，不代表已实时联网）：\n'+'\n'.join(note['sources'])+'\n版本：'+note['revision']
        try:old=store.read(identity)
        except ValueError:old=None
        if old:decision='reused' if old['body']==body and old['title']==note['title'] else 'preserved_user_version'
        else:
            decision='imported' if apply else 'would_import'
            if apply:store.save({'id':identity,'title':note['title'],'body':body})
        rows.append({'id':key,'decision':decision})
    return rows


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data-dir',required=True);p.add_argument('--apply',action='store_true');a=p.parse_args()
    print(json.dumps(install(KnowledgeStore(RuntimeDatabase(Path(a.data_dir)/'metadata')),a.apply),ensure_ascii=False))
