'use strict';
let csrf='', selected=null, polling=false, offline=true, recoveryRequired=false, currentDetail=null;
const $=id=>document.getElementById(id);
const names={running:'运行中',succeeded:'成功',failed:'失败',waiting:'等待',timeout:'超时',skipped:'已跳过',unschedulable:'预算不足',cancelled:'已停止',cancelling:'停止中',recovery_required:'待管理员恢复'};
const scenes={resources:'资源分配',dynamic:'动态协作',ipc:'Agent 通信',document:'文档处理',llm:'DeepSeek 对话',team:'四 Agent 协作',review:'写作与评审',engineering:'Python 项目修复'};
const palette=['#109a8c','#5d8dcc','#c79d52','#957bb7','#5fa0a6','#c17c8b','#709563'];
const finite=value=>typeof value==='number'&&Number.isFinite(value);
const number=(value,digits=1)=>finite(value)?value.toFixed(digits):'—';
function bytes(value){if(!finite(value))return '—';if(value>=1024**3)return number(value/1024**3,2)+' GiB';if(value>=1024**2)return number(value/1024**2,1)+' MiB';if(value>=1024)return number(value/1024,1)+' KiB';return Math.round(value)+' B'}
function formatResult(value){if(value==null)return '（无结果）';return typeof value==='object'?JSON.stringify(value,null,2):String(value)}
function node(tag,text,className){const e=document.createElement(tag);if(text!==undefined&&text!==null)e.textContent=String(text);if(className)e.className=className;return e}
function badge(state){return node('span',names[state]||state,'badge '+state)}
function empty(text,compact=false){return node('div',text,'empty'+(compact?' compact':''))}
function colorFor(id){let value=0;for(const c of id)value=(value*31+c.charCodeAt(0))>>>0;return palette[value%palette.length]}
function meter(id,value,total=100){$(id).style.width=finite(value)&&finite(total)&&total>0?Math.max(0,Math.min(100,value/total*100))+'%':'0%'}
function metric(id,value,unit){const box=$(id);box.replaceChildren(node('span',value));if(unit)box.append(node('small',unit))}
async function api(path,body){
  const controller=new AbortController();
  const deadline=setTimeout(()=>controller.abort(),8000);
  const options=body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json','X-AgentRT-CSRF':csrf},body:JSON.stringify(body)};
  try{
    const response=await fetch('api/'+path,{...options,signal:controller.signal});
    if(!response.ok){let result;try{result=await response.json()}catch{throw Error('连接失败，请刷新或重新登录')}throw Error(result.error||'请求失败')}
    return await response.json();
  }catch(error){if(error.name==='AbortError')throw Error('控制台请求超时');throw error}
  finally{clearTimeout(deadline)}
}

const projectDescriptions={pricing:'三个整数金额函数：修复折扣、免邮阈值与退款舍入，保留输入校验。',intervals:'三个时间工具函数：检查半开区间、时间分桶和剩余 TTL 的边界。',text_normalization:'三个文本函数：统一空白与大小写，准确统计词数并检查前缀。'};
function updateScene(){
  const scene=$('preset').value;
  const hints={resources:'约 15 秒的真实小负载：观察排队、CPU/内存使用、配额分配和任务结束后的释放。无需模型密钥。',dynamic:'固定规划 JSON：两个任务并行，其中一个失败后重试，最终汇总。无需模型密钥。',ipc:'独立 Worker 通过本机 mailbox 交换上下文引用。无需模型密钥。',document:'粘贴一段文本，分析和索引 Agent 并行处理后汇总。留空使用示例文本。',llm:'一次模型调用，只返回文本，会消耗所配置账户的 API 额度。',team:'规划 → 分析与测试并行 → 汇总，共四次模型调用，会消耗 API 额度。',review:'写作 → 评审 → 根据反馈修订；最多两轮、四次模型调用，会消耗 API 额度。',engineering:'原始失败 → 分析 / 测试设计 → 补丁 → 真实测试 → 最终评审。最多 4 次模型调用，最大输出共 2048 token，输入另计；失败停止，不自动重试。'};
  $('promptField').hidden=!['document','llm','team','review'].includes(scene);
  if($('projectField'))$('projectField').hidden=scene!=='engineering';
  if($('engineeringBudgetNote'))$('engineeringBudgetNote').hidden=scene!=='engineering';
  if($('projectDescription'))$('projectDescription').textContent=projectDescriptions[$('project')?.value]||'';
  $('sceneHint').textContent=hints[scene]||'';
  $('submit').firstElementChild && ($('submit').firstElementChild.textContent=scene==='engineering'?'运行修复与验证':'启动工作流');
}

function renderMachine(machine={}){
  const host=machine.host||{},service=machine.service||{};
  metric('hostCpu',number(host.cpu_percent,1),finite(host.cpu_percent)?'%':'');
  $('hostCpuInfo').textContent=finite(host.logical_cpus)?host.logical_cpus+' 个逻辑 CPU · 整机整体利用率':'等待 Linux 采样';
  meter('hostCpuMeter',host.cpu_percent);
  metric('hostMemory',bytes(host.memory_used_bytes));
  $('hostMemoryInfo').textContent=finite(host.memory_total_bytes)?'物理内存共 '+bytes(host.memory_total_bytes):'实际内存采样不可用';
  meter('hostMemoryMeter',host.memory_used_bytes,host.memory_total_bytes);
  metric('serviceCpu',number(service.cpu_cores,2),finite(service.cpu_cores)?'核':'');
  $('serviceCpuInfo').textContent=finite(service.cpu_limit_cores)?'服务上限 '+number(service.cpu_limit_cores,2)+' 核 · 含控制进程与 Worker':'服务范围的 cgroup 采样不可用';
  meter('serviceCpuMeter',service.cpu_cores,service.cpu_limit_cores);
  metric('serviceMemory',bytes(service.memory_bytes));
  $('serviceMemoryInfo').textContent=finite(service.memory_limit_bytes)?'服务上限 '+bytes(service.memory_limit_bytes)+' · 当前 '+(finite(service.pids)?service.pids:'—')+' 个进程/线程':'服务范围的 cgroup 采样不可用';
  meter('serviceMemoryMeter',service.memory_bytes,service.memory_limit_bytes);
  $('updated').textContent=finite(machine.time)?'采样 '+new Date(machine.time*1000).toLocaleTimeString():'等待采样';
}

function renderJobs(jobs){const list=$('jobs');list.replaceChildren();$('jobCount').textContent=jobs.length+' 次';if(!jobs.length)list.append(empty('暂无运行记录',true));for(const job of jobs){const button=node('button',null,'job'+(selected===job.id?' selected':''));button.type='button';const row=node('div',null,'job-row');const projectLabel={pricing:'金额计算',intervals:'时间区间',text_normalization:'文本规范化'};const title=job.preset==='engineering'?'项目修复 · '+(projectLabel[job.project]||job.project||'受控项目'):scenes[job.preset]||job.preset;row.append(node('span',title),badge(job.status));button.append(row,node('span',job.id.slice(0,10)+' · '+new Date(job.created*1000).toLocaleTimeString(),'mono'));button.onclick=async()=>{selected=job.id;await refresh()};list.append(button)}}

function renderAllocations(monitor){const box=$('allocations');box.replaceChildren();if(!monitor){box.append(empty('正在等待 Runtime 的资源账本'));return}const rows=monitor.tasks||[],running=monitor.stale?[]:rows.filter(t=>t.state==='running');const specs=[['CPU 预留','cpu_milli',v=>number(v/1000,2)+' 核'],['内存预留','memory_bytes',bytes],['PIDs 预留','pids',v=>String(v)],['Worker 槽位','slots',v=>String(v)]];for(const [label,key,format] of specs){const reserved=monitor.stale?null:(key==='slots'?monitor.slots_used:monitor.reserved?.[key]),budget=key==='slots'?monitor.slots_total:monitor.budget?.[key];const card=node('div');const head=node('div',null,'allocation-head');head.append(node('span',label),node('span',finite(reserved)&&finite(budget)&&budget>0?Math.round(reserved/budget*100)+'%':'—'));const value=node('div',finite(reserved)?format(reserved):'—','allocation-value');value.append(node('small',' / '+(finite(budget)?format(budget):'—')));const track=node('div',null,'allocation-track');if(finite(budget)&&budget>0){for(const task of running){const amount=key==='slots'?1:task.resources?.[key];if(!finite(amount))continue;const segment=node('i');segment.style.width=Math.max(0,Math.min(100,amount/budget*100))+'%';segment.style.background=colorFor(task.id);segment.title=task.id+'：'+format(amount);track.append(segment)}}card.append(head,value,track,node('div',monitor.stale?'采样失效 · 分配状态未知':(key==='slots'?'正在占用 / 并发上限':'调度器账本 · 不是实际消耗'),'allocation-sub'));box.append(card)}}

function svgNode(tag,attrs={},text){const item=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [key,value] of Object.entries(attrs))item.setAttribute(key,String(value));if(text!==undefined)item.textContent=String(text);return item}
function chart(container,history,actual,reserved,budget,format,color){
  const box=$(container);box.replaceChildren();const svg=svgNode('svg',{viewBox:'0 0 450 150',preserveAspectRatio:'none',role:'img','aria-label':container==='cpuChart'?'Worker CPU实际与预留曲线':'Worker内存实际与预留曲线'});box.append(svg);
  const left=4,right=448,top=16,bottom=132;
  const values=history.flatMap(frame=>[actual(frame),reserved(frame)]).filter(finite);
  const max=Math.max(finite(budget)?budget:0,...values,container==='cpuChart'?1:1024**2);
  for(let i=0;i<=4;i++){const y=top+(bottom-top)*i/4;svg.append(svgNode('line',{x1:left,y1:y,x2:right,y2:y,stroke:'#e6eef0','stroke-width':'.7'}))}
  for(let i=0;i<=6;i++){const x=left+(right-left)*i/6;svg.append(svgNode('line',{x1:x,y1:top,x2:x,y2:bottom,stroke:'#edf2f4','stroke-width':'.7'}))}
  svg.append(svgNode('text',{x:right,y:10,'text-anchor':'end',fill:'#8ea3aa','font-size':8},format(max)));
  if(!history.length){svg.append(svgNode('text',{x:225,y:79,'text-anchor':'middle',fill:'#9daeb4','font-size':11},'等待真实采样'));return}
  const first=history[0].time,last=history[history.length-1].time,span=Math.max(.4,last-first);
  function paths(selector){let path='',segments=[];for(const frame of history){const value=selector(frame);if(!finite(value)){if(path)segments.push(path);path='';continue}const x=left+(frame.time-first)/span*(right-left),y=bottom-Math.max(0,value)/max*(bottom-top);path+=(path?' L':'M')+x.toFixed(2)+','+y.toFixed(2)}if(path)segments.push(path);return segments}
  for(const d of paths(reserved))svg.append(svgNode('path',{d,fill:'none',stroke:'#d4a156','stroke-width':1.5,'stroke-dasharray':'4 3','vector-effect':'non-scaling-stroke'}));
  for(const d of paths(actual))svg.append(svgNode('path',{d,fill:'none',stroke:color,'stroke-width':2,'stroke-linejoin':'round','vector-effect':'non-scaling-stroke'}));
  if(history.length===1){const v=actual(history[0]);if(finite(v))svg.append(svgNode('circle',{cx:left,cy:bottom-v/max*(bottom-top),r:2,fill:color}))}
  svg.append(svgNode('text',{x:left,y:147,fill:'#9babb2','font-size':8},'0 s'),svgNode('text',{x:right,y:147,'text-anchor':'end',fill:'#9babb2','font-size':8},number(last-first,1)+' s'));
}

// Missing measurements remain gaps; zero after completion is valid only when no Worker remains.
function renderCharts(monitor,history){const latest=history[history.length-1]||{};$('cpuReadout').textContent=monitor?.stale?'— · 采样已失效':(finite(latest.cpu_cores)?number(latest.cpu_cores,2)+' 核实际':'实际 —');$('memoryReadout').textContent=monitor?.stale?'— · 采样已失效':bytes(latest.memory_bytes)+' 实际';chart('cpuChart',history,f=>f.cpu_cores,f=>finite(f.reserved?.cpu_milli)?f.reserved.cpu_milli/1000:null,monitor?.budget?.cpu_milli/1000,v=>number(v,2)+' 核','#078681');chart('memoryChart',history,f=>f.memory_bytes,f=>f.reserved?.memory_bytes,monitor?.budget?.memory_bytes,bytes,'#4f94be');const span=history.length>1?number(history[history.length-1].time-history[0].time,1)+' 秒记录':'等待采样';$('cpuRange').textContent=(monitor?.stale?'最后记录 · ':'')+span;$('memoryRange').textContent=(monitor?.stale?'最后记录 · ':'')+span;let note='曲线属于选中的工作流；顶部指标属于整机或整个 Runtime 服务。';if(monitor&&monitor.enforcement!=='cgroup_v2')note+=' 当前仅资源准入，Worker 的 OS 实际用量未采集。';if(monitor?.scheduling?.draining_task_id)note+=' 老化保护：为 '+monitor.scheduling.draining_task_id+' 等待资源释放。';if(monitor?.stale)note='采样已失效，资源回收状态需管理员确认。图表仅保留最后记录，当前资源用量与预留状态未知。';$('sampleNote').className=monitor?.stale?'hint stale-warning':'hint';$('sampleNote').textContent=note}

function taskRows(job){if(job.monitor?.tasks?.length)return job.monitor.tasks;return Object.entries(job.states||{}).map(([id,state])=>{const events=(job.events||[]).filter(e=>e.task_id===id),last=events[events.length-1]||{},start=[...events].reverse().find(e=>e.state==='running')||{};return{id,agent:last.agent||id,state,attempt:last.attempt||0,pid:state==='running'?start.pid:null,resources:start.resources||{},metrics:{},depends_on:[],wait_reason:''}})}
function renderTasks(rows,events,stale=false){const box=$('tasks');box.replaceChildren();$('taskCount').textContent=rows.length+' 个任务 · '+(stale?'最后快照，非实时':rows.filter(t=>t.state==='running').length+' 个运行中');if(!rows.length){box.append(empty('等待 Runtime 建立任务图'));return}const table=node('table'),head=node('tr');for(const title of ['任务 / Agent','状态 / 等待原因','Attempt / PID','CPU 实际 / 配额','内存实际 / 配额','进程数'])head.append(node('th',title));const thead=node('thead');thead.append(head);table.append(thead);const body=node('tbody');for(const task of rows){const row=node('tr'),identity=node('td'),state=node('td'),process=node('td'),cpu=node('td'),memory=node('td'),pids=node('td');const marker=node('i',null,'task-color');marker.style.background=colorFor(task.id);identity.append(marker,node('span',task.id,'task-name'),node('small',task.agent||task.id));state.append(badge(task.state));if(task.wait_reason)state.append(node('small',task.wait_reason));const start=[...events].reverse().find(e=>e.task_id===task.id&&e.state==='running'&&e.attempt===task.attempt);process.append(node('span','第 '+(task.attempt||0)+' 次'),node('small',(task.pid||start?.pid)?'PID '+(task.pid||start.pid):'尚未分配 PID'));const metrics=stale?{...task.metrics,cpu_cores:null,memory_bytes:null,pids:null}:(task.metrics||{}),limits=task.resources||{};cpu.append(node('span',finite(metrics.cpu_cores)?number(metrics.cpu_cores,2)+' 核':'—'),node('small','配额 '+(finite(limits.cpu_milli)?number(limits.cpu_milli/1000,2)+' 核':'—')));const live=task.state==='running',terminal=['failed','timeout','skipped','unschedulable','cancelled','succeeded'].includes(task.state);memory.append(node('span',bytes(live?metrics.memory_bytes:(terminal?metrics.memory_peak_bytes:null))),node('small',(terminal?'峰值 / 配额 ':'配额 ')+bytes(limits.memory_bytes)));pids.append(node('span',finite(metrics.pids)?metrics.pids:'—'),node('small','配额 '+(finite(limits.pids)?limits.pids:'—')));if(finite(metrics.cpu_total_seconds))cpu.title='累计 CPU '+number(metrics.cpu_total_seconds,3)+' 秒；节流周期 '+(metrics.throttled_periods??'—');row.append(identity,state,process,cpu,memory,pids);body.append(row)}table.append(body);box.append(table)}

function dependencyLevels(rows){const map=new Map(rows.map(row=>[row.id,row])),memo=new Map();function level(id,seen=new Set()){if(memo.has(id))return memo.get(id);if(seen.has(id))return 0;seen.add(id);const deps=(map.get(id)?.depends_on||[]).filter(dep=>map.has(dep));const value=deps.length?1+Math.max(...deps.map(dep=>level(dep,new Set(seen)))):0;memo.set(id,value);return value}return rows.map(row=>({row,level:level(row.id)}))}
function renderDependencies(rows){const box=$('dependencyGraph');box.replaceChildren();if(!rows.length){box.append(empty('暂无任务图',true));return}const levels=dependencyLevels(rows),max=Math.max(...levels.map(item=>item.level));for(let level=0;level<=max;level++){const column=node('div',null,'dependency-column');for(const {row} of levels.filter(item=>item.level===level)){const card=node('div',null,'dependency-node '+row.state);card.append(node('strong',row.id),badge(row.state));card.append(node('p',(row.depends_on||[]).length?'依赖：'+row.depends_on.join('、'):'入口任务'));if(row.state==='waiting'&&row.wait_reason)card.append(node('p',row.wait_reason));column.append(card)}box.append(column)}}

function attemptsFrom(events,end){const attempts=new Map();for(const event of events){if(!event.task_id||!event.attempt)continue;const key=event.task_id+'/'+event.attempt;if(event.state==='running'){attempts.set(key,{task:event.task_id,attempt:event.attempt,pid:event.pid,start:event.time,end:null,state:'running'})}else if(attempts.has(key)&&['succeeded','failed','timeout','cancelled','skipped','unschedulable','waiting'].includes(event.state)){const item=attempts.get(key);item.end=event.time;item.state=event.state==='waiting'?'failed':event.state}}return [...attempts.values()].map(item=>({...item,end:item.end??end})).sort((a,b)=>a.start-b.start)}
function renderTimeline(job){const box=$('timeline');box.replaceChildren();const events=job.events||[],active=['running','cancelling'].includes(job.status),end=active?(job.monitor?.time||Date.now()/1000):(job.finished||events[events.length-1]?.time||job.created);const attempts=attemptsFrom(events,end);if(!attempts.length){box.append(empty('任务启动后显示真实执行区间',true));$('timelineDuration').textContent='—';return}const first=Math.min(...attempts.map(item=>item.start)),last=Math.max(first+.1,...attempts.map(item=>item.end)),span=last-first;$('timelineDuration').textContent=number(span,1)+' 秒';for(const item of attempts){const row=node('div',null,'timeline-row'),label=node('div',null,'timeline-label'),track=node('div',null,'timeline-track'),bar=node('i',null,'timeline-bar '+(job.monitor?.stale&&item.state==='running'?'stale':item.state));label.append(node('b',item.task),document.createTextNode(' · #'+item.attempt));label.title=item.task+' / Attempt '+item.attempt+' / PID '+item.pid;bar.style.left=Math.max(0,(item.start-first)/span*100)+'%';bar.style.width=Math.max(.35,(item.end-item.start)/span*100)+'%';bar.style.backgroundColor=colorFor(item.task);bar.title=(names[item.state]||item.state)+' · PID '+item.pid+' · '+number(item.end-item.start,2)+' 秒';track.append(bar);row.append(label,track,node('div',number(item.end-item.start,1)+'s','timeline-duration'));box.append(row)}const axis=node('div',null,'timeline-axis');axis.append(node('span','0 s'),node('span',number(span/2,1)+' s'),node('span',number(span,1)+' s'));box.append(axis)}

function renderContext(monitor){const stats=monitor?.context||{},box=$('contextCacheStats');box.replaceChildren();const count=value=>finite(value)?String(value):'—';const items=[['命中 / 未命中',count(stats.cache_hits)+' / '+count(stats.cache_misses)],['磁盘读取',count(stats.disk_reads)],['缓存淘汰',count(stats.cache_evictions)],['占用 / 容量',bytes(stats.cache_bytes)+' / '+bytes(stats.cache_limit_bytes)]];for(const [label,value] of items){const card=node('div',null,'cache-stat');card.append(node('span',label),node('b',value));box.append(card)}$('contextCacheNote').textContent=(monitor?.stale?'最后记录 · ':'')+'应用层 JSON 缓存 · 不是模型 KV Cache'}

function engineeringCounts(report){
  if(!report||typeof report!=='object')return null;
  const candidate=report.candidate&&typeof report.candidate==='object'?report.candidate:report;
  return {public:candidate.public||report.public,heldout:candidate.heldout||report.heldout};
}
function testCountLine(label,counts){
  const row=node('div',null,'repair-check-row');
  if(!counts||typeof counts!=='object'){row.append(node('span',label),node('b','待检查','muted'));return row}
  const failed=(finite(counts.failed)?counts.failed:0)+(finite(counts.errors)?counts.errors:0);
  const total=finite(counts.total)?counts.total:finite(counts.tests_run)?counts.tests_run:null;
  const passed=finite(counts.passed)?counts.passed:null;
  row.className+=' '+(failed>0?'check-fail':passed!==null&&total!==null&&passed===total?'check-pass':'');
  row.append(node('span',label),node('b',(passed===null?'—':passed)+' / '+(total===null?'—':total)+' 通过'));
  if(failed>0)row.append(node('small',failed+' 项失败 / 错误'));
  return row;
}
function renderCheckGroup(id,title,report,baseline=false){
  const box=$(id);if(!box)return;box.replaceChildren(node('h3',title));
  const counts=engineeringCounts(report);
  box.append(testCountLine('公开用例',counts?.public),testCountLine('独立用例',counts?.heldout));
  if(baseline&&finite(report?.baseline_failing_count))box.append(node('p','原版 '+report.baseline_failing_count+' 项失败（预期红灯）','hint'));
}
function renderEngineering(job){
  const panel=$('engineeringPanel');if(!panel)return;
  const engineering=job.engineering;
  panel.hidden=job.preset!=='engineering'&&!engineering;
  if(panel.hidden)return;
  const data=engineering||{},stale=Boolean(job.monitor?.stale),project=data.project||job.project||'pricing';
  const labels={pricing:'金额计算',intervals:'时间区间',text_normalization:'文本规范化'};
  $('engineeringTitle').textContent=data.title||'Python 项目修复 · '+(labels[project]||project);
  const validation=data.validation,verdict=data.verdict;
  const semanticFailure=validation?.success===false||verdict?.accepted===false;
  const success=verdict?.accepted===true&&validation?.success===true;
  const outcome=stale?'最后记录 · 非实时':success?'修复通过':semanticFailure||job.status==='failed'?'业务未通过':names[job.status]||'等待启动';
  $('engineeringOutcome').textContent=outcome;
  $('engineeringOutcome').className='badge '+(stale?'waiting':success?'succeeded':semanticFailure||job.status==='failed'?'failed':job.status);
  const phases=Array.isArray(data.phases)?data.phases:[];
  const defaults=[['baseline','原始失败'],['analyze','问题分析'],['test_plan','测试设计'],['repair','模型补丁'],['validate','真实测试'],['review','最终评审'],['gate','结果确认']];
  const box=$('engineeringPhases');box.replaceChildren();
  for(const [id,label] of defaults){
    const phase=phases.find(p=>p.id===id)||{};
    const state=phase.status||job.states?.[id]||'waiting';
    const allowed=['waiting','running','succeeded','failed','timeout','skipped','cancelled','unschedulable'];
    const card=node('div',null,'repair-phase '+(allowed.includes(state)?state:'waiting'));
    const head=node('div',null,'repair-phase-head');head.append(node('strong',phase.label||label),badge(state));card.append(head);
    if(id==='baseline')card.append(node('small','确认缺陷，不要求原版通过'));
    if(phase.details!=null){const details=node('details',null,'repair-phase-details');details.append(node('summary','查看阶段记录'),node('pre',typeof phase.details==='string'?phase.details:formatResult(phase.details)));card.append(details)}
    box.append(card);
  }
  renderCheckGroup('engineeringBaseline','修复前 · 预期失败',data.baseline,true);
  renderCheckGroup('engineeringValidation','修复后 · 真实检查',validation);
  const decision=$('engineeringVerdict');decision.replaceChildren();
  if(stale)decision.append(node('strong','最后记录；连接或执行状态未确认'));
  else if(verdict&&typeof verdict==='object'){
    decision.append(node('strong',verdict.accepted===true?'最终评审接受':verdict.accepted===false?'最终评审未接受':'最终评审已返回'));
    if(verdict.reason)decision.append(node('p',verdict.reason));
  }else decision.append(node('span',semanticFailure?'验证未通过，停止后续付费调用':'等待真实测试和最终评审结果'));
  decision.className='repair-verdict '+(success?'check-pass':semanticFailure?'check-fail':'');
  $('engineeringCalls').textContent='模型调用 '+(finite(data.model_calls)?data.model_calls:'—')+' / '+(finite(data.call_limit)?data.call_limit:4);
  const usage=data.usage||{};$('engineeringTokens').textContent='输入 / 输出 token：'+(finite(usage.prompt_tokens)?usage.prompt_tokens:'—')+' / '+(finite(usage.completion_tokens)?usage.completion_tokens:'—');
  const budget=data.budget||job.budget||{};$('engineeringBudget').textContent='固定预算：'+(finite(budget.cpu_milli)?number(budget.cpu_milli/1000,2)+' 核':'—')+' / '+bytes(budget.memory_bytes)+' / '+(finite(budget.pids)?budget.pids:'—')+' pids';
  const source=typeof data.patch_source==='string'?data.patch_source:typeof validation?.source==='string'?validation.source:'';
  $('engineeringPatch').hidden=!source;$('engineeringSource').textContent=source;
  $('engineeringHash').textContent=data.source_sha256||validation?.source_sha256||'';
}

function renderDetail(job){currentDetail=job;renderEngineering(job);$('jobId').textContent=job.id+' · '+(names[job.status]||job.status);$('cancel').hidden=!['running','cancelling'].includes(job.status);$('cancel').disabled=job.status==='cancelling';const rows=taskRows(job);renderAllocations(job.monitor);renderContext(job.monitor);renderCharts(job.monitor,job.history||[]);renderTasks(rows,job.events||[],Boolean(job.monitor?.stale));renderDependencies(rows);renderTimeline(job);const events=job.events||[];$('eventCount').textContent=events.length+' 条';$('events').textContent=events.length?events.map(e=>new Date(e.time*1000).toLocaleTimeString()+'  '+e.task_id+'  #'+e.attempt+'  '+(names[e.state]||e.state)+(e.reason?' · '+e.reason:'')+(e.error?' · '+e.error:'')).join('\n'):'等待第一个事件…';const outcomes=events.filter(e=>e.state==='succeeded'&&e.result);$('answer').hidden=!outcomes.length;$('answerText').textContent=outcomes.map(e=>e.task_id+': '+formatResult(e.result.result)).join('\n\n');if(job.error)$('notice').textContent=job.error}

function markOffline(message){
  offline=true;
  $('connection').textContent='连接中断 · 数据已失效';
  $('connection').className='connection recovery';
  renderMachine({});
  $('updated').textContent='当前采样不可用';
  if(currentDetail){const monitor={budget:currentDetail.budget,...currentDetail.monitor,stale:true};renderDetail({...currentDetail,monitor})}
  else{renderAllocations({stale:true});renderCharts({stale:true},[])}
  $('submit').disabled=true;$('cancel').disabled=true;$('saveKey').disabled=true;
  $('notice').textContent=message+'；当前资源读数未知，请等待连接恢复。';
}
async function refresh(){
  if(polling)return;
  polling=true;
  try{
    const state=await api('status');
    csrf=state.csrf;offline=false;recoveryRequired=Boolean(state.recovery_required);
    $('connection').textContent=recoveryRequired?'● 已连接 · 待管理员恢复':'● Runtime 已连接';
    $('connection').className=recoveryRequired?'connection recovery':'connection';
    const busy=state.jobs.some(job=>['running','cancelling'].includes(job.status));
    $('runtimeState').textContent=recoveryRequired?'需要管理员恢复':busy?'执行中':'就绪';
    $('enforcement').textContent=state.enforcement==='cgroup_v2'?'cgroups v2 · 硬限制':'仅预算准入';
    $('modelState').textContent=state.model_available===false?'模型服务暂不可用':state.model_configured?state.model:'待配置密钥';
    $('submit').disabled=busy||recoveryRequired;
    $('saveKey').disabled=busy||recoveryRequired;
    renderMachine(state.machine||{});
    if(!selected&&state.jobs.length)selected=state.jobs[0].id;
    if(selected&&!state.jobs.some(job=>job.id===selected))selected=state.jobs[0]?.id||null;
    renderJobs(state.jobs);
    if(selected)renderDetail(await api('jobs/'+selected));
    if(recoveryRequired){$('notice').textContent=state.recovery_message||'资源回收状态未确认，请管理员检查后恢复服务';$('cancel').disabled=true}
    else if($('notice').textContent.includes('当前资源读数未知'))$('notice').textContent='';
  }catch(error){markOffline(error.message)}finally{polling=false}
}

function initialize(){
  $('preset').onchange=updateScene;
  if($('project'))$('project').onchange=updateScene;
  $('saveKey').onclick=async()=>{if(offline||recoveryRequired)return;const key=$('apiKey').value.trim();$('keyNotice').textContent='';$('saveKey').disabled=true;try{await api('model-config',{key});$('apiKey').value='';$('keyNotice').textContent=key?'已保存到模型服务内存':'已清除密钥';await refresh()}catch(error){$('keyNotice').textContent=error.message}finally{$('saveKey').disabled=offline||recoveryRequired}};
  $('form').onsubmit=async event=>{event.preventDefault();if(offline||recoveryRequired)return;$('notice').textContent='';$('submit').disabled=true;try{const result=await api('jobs',{preset:$('preset').value,workers:Number($('workers').value),policy:$('policy').value,project:$('project')?.value||'pricing',prompt:$('preset').value==='engineering'?'':$('prompt').value});selected=result.id;await refresh()}catch(error){$('notice').textContent=error.message}finally{await refresh()}};
  $('cancel').onclick=async()=>{if(!selected||offline||recoveryRequired)return;try{await api('jobs/'+selected+'/cancel',{});await refresh()}catch(error){$('notice').textContent=error.message}};
  updateScene();$('saveKey').disabled=true;renderCharts(null,[]);renderContext(null);refresh();setInterval(refresh,1000);
}
if(typeof document!=='undefined'&&document.getElementById('form'))initialize();
