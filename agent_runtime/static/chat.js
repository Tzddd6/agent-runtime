'use strict';
const $=id=>document.getElementById(id);
let selected=null,current=null,status=null,csrf='',polling=false,sending=false,signature='';
const processOpen=new Map();
const modes={llm:'直接对话',team:'多 Agent 协作',review:'写作与评审',document:'文本分析',engineering:'示例项目修复'};
const active=t=>t.status==='running';
const uuid=()=>crypto.randomUUID().replaceAll('-','');
function element(tag,text,cls){const el=document.createElement(tag);if(text!=null)el.textContent=text;if(cls)el.className=cls;return el}
function notify(text){$('notice').textContent=text;$('notice').hidden=!text}
async function api(path,body){
  const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),10000);
  try{
    const response=await fetch('api/'+path,{signal:abort.signal,cache:'no-store',...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-AgentRT-CSRF':csrf},body:JSON.stringify(body)})});
    let value;try{value=await response.json()}catch{throw Error('无法读取服务响应，请重新登录或打开控制台核对')}
    if(!response.ok){const error=Error(value.error||'请求失败');error.status=response.status;throw error}return value;
  }catch(error){if(error.name==='AbortError')throw Error('请求超时，请核对记录，不会自动重发');throw error}
  finally{clearTimeout(timer)}
}
function address(id){const url=new URL(location.href);if(id)url.searchParams.set('chat',id);else url.searchParams.delete('chat');history.replaceState(null,'',url);try{if(id)localStorage.setItem('agentrt-chat',id);else localStorage.removeItem('agentrt-chat')}catch{}}
function closeMenu(){$('sidebar').classList.remove('open');$('shade').hidden=true}
function newChat(){selected=null;current=null;signature='';address(null);$('messages').replaceChildren();$('messages').hidden=true;$('welcome').hidden=false;$('consoleLink').href='console';notify('');closeMenu();controls();$('prompt').focus()}
async function selectChat(id){selected=id;current=null;signature='';address(id);closeMenu();notify('');$('welcome').hidden=true;$('messages').hidden=false;$('messages').replaceChildren(element('p','正在读取对话…','muted'));await refresh()}
function showHistory(list){
  const box=$('chatHistory');box.replaceChildren();if(!list.length)box.append(element('p','新的对话会保存在这里','muted'));
  for(const chat of list){const button=element('button',chat.title,'history-item'+(chat.id===selected?' active':''));button.type='button';button.title=chat.title;button.setAttribute('aria-current',String(chat.id===selected));button.onclick=()=>selectChat(chat.id);box.append(button)}
}
function copy(text,button){navigator.clipboard.writeText(text).then(()=>{button.textContent='已复制'},()=>{notify('复制失败，请手动选中文本复制')})}
function bodyText(box,text){
  renderMarkdown(box,text,copy);
}
function processPanel(turn){
  const execution=turn.execution,working=active(turn),view=element('details',null,'process-panel'+(working?' working':''));
  view.open=processOpen.has(turn.request_id)?processOpen.get(turn.request_id):working;
  view.ontoggle=()=>processOpen.set(turn.request_id,view.open);
  const labels={running:'正在处理',cancelling:'正在停止',succeeded:'处理完成',failed:'执行未完成',cancelled:'已停止',interrupted:'运行已中断',recovery_required:'需要管理员检查'};
  const elapsed=execution?.elapsed_seconds,summary=element('summary');
  summary.append(element('span',working?'◎':turn.status==='succeeded'?'✓':'!','process-icon'),
                 element('span',(labels[turn.status]||turn.status)+(typeof elapsed==='number'?' · '+elapsed.toFixed(1)+' 秒':'')),element('span','⌄','process-chevron'));
  view.append(summary);
  const stages=element('div',null,'process-stages'),states={waiting:'等待',running:'执行中',succeeded:'完成',failed:'失败',cancelled:'停止',timeout:'超时',skipped:'跳过',unschedulable:'预算不足',retrying:'准备重试',interrupted:'已中断'};
  if(execution?.steps?.length){
    for(const step of execution.steps){const row=element('div',null,'process-step '+step.status);
      const description=element('div',null,'process-step-label');description.append(element('span',step.label),element('small',(step.kind==='model'?'模型请求':'本地执行')+(step.attempt>1?' · 第 '+step.attempt+' 次尝试':'')));
      row.append(element('span',step.status==='succeeded'?'✓':step.status==='running'?'◌':'·','process-step-icon'),description,
                 element('span',(states[step.status]||step.status)+(typeof step.elapsed_seconds==='number'?' · '+step.elapsed_seconds.toFixed(2)+' 秒':''),'process-step-time'));stages.append(row)}
  }else stages.append(element('p',working?'请求已提交，正在等待执行记录。':'该历史记录没有步骤快照，可打开执行记录查看。','process-note'));
  stages.append(element('p','展示实际执行步骤与耗时，不包含模型内部思考内容。'+(typeof execution?.model_admitted==='number'?'已放行模型请求 '+execution.model_admitted+' 次。':''),'process-note'));
  view.append(stages);return view;
}
function render(chat){
  current=chat;const last=chat.turns.at(-1);
  $('consoleLink').href=last?.job_id?'console?job='+encodeURIComponent(last.job_id):'console';
  const next=JSON.stringify(chat);if(next===signature){controls();return}signature=next;
  const follow=window.innerHeight+window.scrollY>=document.body.scrollHeight-160;
  const box=$('messages');box.replaceChildren();$('welcome').hidden=chat.turns.length>0;box.hidden=!chat.turns.length;
  for(const turn of chat.turns){
    const user=element('article',null,'message user');user.setAttribute('aria-label','你的消息');user.append(element('div',turn.prompt,'user-bubble'));box.append(user);
    const assistant=element('article',null,'message assistant');assistant.setAttribute('aria-label','Agent Runtime 的回答');const heading=element('div',null,'assistant-heading');heading.append(element('span','✳'),element('span','Agent Runtime'),element('span',modes[turn.mode]||''));assistant.append(heading);
    assistant.append(processPanel(turn));
    const body=element('div',null,'assistant-body');
    if(active(turn)){
      const pending=element('div',null,'pending');pending.append(element('span',null,'working-dot'),element('span','等待本次执行完成后显示回答…'));body.append(pending);
    }else bodyText(body,turn.answer||'此运行没有可用回答，请在控制台查看记录。');
    assistant.append(body);
    const actions=element('div',null,'message-actions');
    if(turn.answer){const button=element('button','复制回答','small-button');button.type='button';button.onclick=()=>copy(turn.answer,button);actions.append(button)}
    if(turn.job_id){const link=element('a','查看执行记录 ↗');link.href='console?job='+encodeURIComponent(turn.job_id);actions.append(link)}
    assistant.append(actions);box.append(assistant);
  }
  controls();if(follow)requestAnimationFrame(()=>window.scrollTo({top:document.body.scrollHeight,behavior:'instant'}));
}
function progressText(turn){const p=turn.progress;if(p?.status==='cancelling')return '正在停止执行…';if(p?.total)return '正在协作，已完成 '+p.finished+' / '+p.total+' 个步骤';return '正在处理你的请求…'}
function controls(){
  const pending=current?.turns.find(active),online=Boolean(status),busy=Boolean(status?.busy)||sending||Boolean(pending),free=$('mode').value==='document';
  $('send').disabled=!online||busy||Boolean(status?.recovery_required)||(!free&&!status?.configured);
  $('send').title=!free&&!status?.configured?'请在运行控制台配置模型密钥':'发送消息';
  $('stop').hidden=!pending?.job_id;$('runStatus').hidden=!pending;
  if(pending){$('runStatusText').textContent=progressText(pending);$('runLink').href='console?job='+encodeURIComponent(pending.job_id||'')}
  $('project').hidden=$('mode').value!=='engineering';
  const hints={llm:'直接对话使用模型 API，可接续最近的对话。',team:'多个 Agent 分工协作，会产生多次模型调用。',review:'先起草，再评审，最多两轮修订。',document:'在本机分析文本，无需模型费用。',engineering:'只运行受控示例项目的修复与测试，会使用模型 API。'};
  $('composerHint').textContent=status?.recovery_required?'资源回收状态待确认，请管理员打开运行控制台检查。':!online?'连接中断，发送已暂停。':!free&&!status?.configured?'模型尚未就绪，请在运行控制台配置；也可选择免费的文本分析。':hints[$('mode').value]+' 不要发送密钥，重要信息请核实。';
}
async function refresh(){
  if(polling)return;polling=true;
  try{
    const [state,list]=await Promise.all([api('chat/status'),api('chat/conversations')]);
    status=state;csrf=state.csrf;$('modelLabel').textContent=state.model||'Agent Runtime';$('connection').textContent=state.recovery_required?'待管理员恢复':state.busy?'协作进行中':'已连接';$('connection').className='connection';
    showHistory(list.conversations);
    if(selected){const target=selected;try{const chat=await api('chat/conversations/'+target);if(target===selected)render(chat)}catch(error){if(error.status!==404)throw error;if(target===selected){current=null;$('welcome').hidden=true;$('messages').hidden=false;$('messages').replaceChildren(element('p','尚未找到会话记录。如刚才的请求状态不明确，请先到控制台核对，不要重复提交。','muted'))}}}
    controls();
  }catch(error){status=null;$('connection').textContent='连接中断';$('connection').className='connection offline';controls();notify(error.message)}
  finally{polling=false}
}
async function send(event){
  event.preventDefault();if($('send').disabled||sending)return;
  const prompt=$('prompt').value.trim();if(!prompt)return;
  const target=selected||uuid();selected=target;address(target);sending=true;notify('');controls();
  const payload={conversation_id:target,request_id:uuid(),prompt,mode:$('mode').value,project:$('project').value,
                 model_budget:{calls:Number($('modelCalls').value),output_tokens:Number($('modelTokens').value)}};
  try{await api('chat/turns',payload);if(selected===target)$('prompt').value='';signature='';await refresh()}
  catch(error){notify(error.message+'。不会自动重复提交。');await refresh()}
  finally{sending=false;controls()}
}
function init(){
  const fromURL=new URLSearchParams(location.search).get('chat');let remembered=null;try{remembered=localStorage.getItem('agentrt-chat')}catch{}
  selected=/^[a-f0-9]{32}$/.test(fromURL||remembered||'')?(fromURL||remembered):null;
  $('newChat').onclick=newChat;$('chatForm').onsubmit=send;$('mode').onchange=controls;
  $('prompt').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();$('chatForm').requestSubmit()}};
  $('stop').onclick=async()=>{const turn=current?.turns.find(active);if(!turn?.job_id)return;$('stop').disabled=true;try{await api('jobs/'+turn.job_id+'/cancel',{});await refresh()}catch(error){notify(error.message)}finally{$('stop').disabled=false}};
  $('menu').onclick=()=>{const open=$('sidebar').classList.toggle('open');$('shade').hidden=!open};$('shade').onclick=closeMenu;
  document.onkeydown=event=>{if(event.key==='Escape'){closeMenu();$('budgetOptions').open=false}};
  for(const button of document.querySelectorAll('[data-prompt]'))button.onclick=()=>{$('mode').value=button.dataset.mode;$('prompt').value=button.dataset.prompt;controls();$('prompt').focus()};
  if(selected){$('welcome').hidden=true;$('messages').hidden=false;$('messages').append(element('p','正在读取对话…','muted'))}
  refresh();setInterval(refresh,1500);
}
init();
