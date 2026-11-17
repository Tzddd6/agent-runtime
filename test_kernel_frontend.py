import shutil,subprocess,unittest
from pathlib import Path


class KernelFrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node.js needed for offline kernel UI validation')
    def test_kernel_panel_resets_and_uses_text_only(self):
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element{constructor(){this.children=[];this._text=''}set textContent(v){this._text=String(v);this.children=[]}get textContent(){return this._text+this.children.map(c=>c.textContent||'').join('')}append(...items){this.children.push(...items)}replaceChildren(...items){this.children=items;this._text=''}set innerHTML(_){throw Error('Unsafe HTML rendering')}}
const nodes=new Map();const document={getElementById(id){if(id==='form')return null;if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id)},createElement(){return new Element()}};
const ctx={document,console,Date};vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),ctx);
const marker='<img src=x onerror=alert(1)>';
ctx.renderKernel({status:'running',kernel:{queue_policy:'round_robin',total_calls:1,queues:{tool:{ready:0,waiting:0,running:0,finished:1,failed:0}},calls:[{id:'call',task_id:marker,attempt:1,service:'tool',operation:'math.calculate',state:'succeeded',pid:123,wait_seconds:.01,execution_seconds:.02}]}});
assert(nodes.get('kernelState').textContent.includes('最后记录'));assert(nodes.get('kernelCalls').textContent.includes(marker));assert(nodes.get('kernelCalls').textContent.includes('math.calculate'));
ctx.renderKernel(null);assert.strictEqual(nodes.get('kernelCalls').children.length,0);assert.strictEqual(nodes.get('kernelQueues').children.length,0);assert(nodes.get('kernelState').textContent.includes('尚无'));
console.log('kernel UI guards passed');
'''
        result=subprocess.run(['node','-',str(Path(__file__).parent/'agent_runtime/static/console.js')],
                              input=script,text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
