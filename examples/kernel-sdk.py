"""Run from repository root: python3 examples/kernel-sdk.py (no model API)."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_runtime.sdk import KernelClient,Request,StorageQuery,ToolQuery

client=KernelClient('runs/kernel-sdk',user_identity='local-demo')
report=client.batch([
    Request('save','writer',StorageQuery('write',{'path':'hello.txt','content':'Hello kernel'})),
    Request('calculate','analyst',ToolQuery('math.calculate',{'expression':'(12+3)*2'})),
    Request('read','reader',StorageQuery('read',{'path':'hello.txt'}),depends_on=('save',))])
print(report['states'])
print(report['responses'])
