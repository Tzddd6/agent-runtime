'use strict';
const $=id=>document.getElementById(id);
let selected=null,current=null,status=null,csrf='',polling=false,sending=false,signature='';
let pendingAttachments=[],uploading=false,librarySignature='';
const processOpen=new Map();
const modes={auto:'自动路由',llm:'直接对话',team:'多 Agent 协作',review:'写作与评审',document:'文本分析',engineering:'示例项目修复'};
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
function showAttachments(){const box=$('attachmentList');box.replaceChildren();for(const file of pendingAttachments){const chip=element('span',null,'attachment-chip');chip.append(element('span',file.name));const remove=element('button','×');remove.type='button';remove.setAttribute('aria-label','移除附件 '+file.name);remove.onclick=()=>{pendingAttachments=pendingAttachments.filter(a=>a.id!==file.id);showAttachments();controls()};chip.append(remove);box.append(chip)}}
async function uploadFiles(){
  const files=Array.from($('fileInput').files||[]);$('fileInput').value='';if(!files.length)return;
  if(files.length+pendingAttachments.length>4){notify('每次最多4个附件，其中最多2张图片');return}
  const target=selected||uuid();selected=target;address(target);uploading=true;controls();notify('正在验证和保存附件…');
  try{for(const file of files){if(file.size>8*1024**2)throw Error('单文件最多8MiB');const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',',2)[1]);reader.onerror=()=>reject(Error('读取文件失败'));reader.readAsDataURL(file)});const result=await api('attachments',{conversation_id:target,name:file.name,data});if(selected===target)pendingAttachments.push(result)}showAttachments();notify('附件已保存，将用于本次模型请求。请勿上传密钥。')}
  catch(error){showAttachments();notify(error.message+'。上传不会自动重试。')}
  finally{uploading=false;controls()}
}
function address(id){const url=new URL(location.href);if(id)url.searchParams.set('chat',id);else url.searchParams.delete('chat');history.replaceState(null,'',url);try{if(id)localStorage.setItem('agentrt-chat',id);else localStorage.removeItem('agentrt-chat')}catch{}}
function closeMenu(){$('sidebar').classList.remove('open');$('shade').hidden=true}
function newChat(){selected=null;current=null;signature='';pendingAttachments=[];showAttachments();address(null);$('messages').replaceChildren();$('messages').hidden=true;$('welcome').hidden=false;$('consoleLink').href='console';notify('');closeMenu();controls();$('prompt').focus()}
async function selectChat(id){selected=id;current=null;signature='';pendingAttachments=[];showAttachments();address(id);closeMenu();notify('');$('welcome').hidden=true;$('messages').hidden=false;$('messages').replaceChildren(element('p','正在读取对话…','muted'));await refresh()}
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
  if(execution?.routing)stages.append(element('p',execution.routing.route==='direct'?'自动路由：直接回答':'自动路由：首轮 '+execution.routing.tasks+' 项任务'+(execution.routing.reason?' · '+execution.routing.reason:''),'process-note'));
  if(execution?.steps?.length){
    for(const step of execution.steps){const row=element('div',null,'process-step '+step.status);
      const description=element('div',null,'process-step-label');description.append(element('span',step.label),element('small',(step.kind==='model'?'模型请求':'本地执行')+(step.thinking_requested?' · 深度思考':'')+(step.reasoning_tokens>0?' · 推理 '+step.reasoning_tokens+' token':'')+(step.attempt>1?' · 第 '+step.attempt+' 次尝试':'')));
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
    const user=element('article',null,'message user');user.setAttribute('aria-label','你的消息');user.append(element('div',turn.prompt+(turn.attachment_names?.length?'\n\n附件：'+turn.attachment_names.join('、'):''),'user-bubble'));box.append(user);
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
    for(const artifact of new Map((turn.execution?.artifacts||[]).map(a=>[a.path,a])).values()){const link=element('a','下载 '+artifact.path);link.href='api/jobs/'+encodeURIComponent(turn.job_id)+'/files/'+artifact.path.split('/').map(encodeURIComponent).join('/')+'?sha='+encodeURIComponent(artifact.sha256);link.download=artifact.path.split('/').at(-1);actions.append(link)}
    assistant.append(actions);box.append(assistant);
  }
  controls();if(follow)requestAnimationFrame(()=>window.scrollTo({top:document.body.scrollHeight,behavior:'instant'}));
}
function progressText(turn){const p=turn.progress;if(p?.status==='cancelling')return '正在停止执行…';if(p?.total)return '正在协作，已完成 '+p.finished+' / '+p.total+' 个步骤';return '正在处理你的请求…'}
function controls(){
  const pending=current?.turns.find(active),online=Boolean(status),busy=Boolean(status?.busy)||sending||Boolean(pending),free=$('mode').value==='document';
  $('send').disabled=!online||busy||uploading||Boolean(status?.recovery_required)||(!free&&!status?.configured);
  $('attachFiles').disabled=!online||busy||uploading;
  $('prompt').required=pendingAttachments.length===0;
  $('deepThinking').disabled=!status?.native_thinking||busy;
  $('send').title=!free&&!status?.configured?'请在运行控制台配置模型密钥':'发送消息';
  $('stop').hidden=!pending?.job_id;$('runStatus').hidden=!pending;
  if(pending){$('runStatusText').textContent=progressText(pending);$('runLink').href='console?job='+encodeURIComponent(pending.job_id||'')}
  $('project').hidden=$('mode').value!=='engineering';
  const hints={auto:'Runtime 自动判断直接回答或分解协作，模型调用会消耗额度。',llm:'直接对话使用模型 API，可接续最近的对话。',team:'多个 Agent 分工协作，会产生多次模型调用。',review:'先起草，再评审，最多两轮修订。',document:'在本机分析文本，无需模型费用。',engineering:'只运行受控示例项目的修复与测试，会使用模型 API。'};
  $('composerHint').textContent=status?.recovery_required?'资源回收状态待确认，请管理员打开运行控制台检查。':!online?'连接中断，发送已暂停。':!free&&!status?.configured?'模型尚未就绪，请在运行控制台配置。':hints[$('mode').value]+' 不要发送密钥，重要信息请核实。';
}
async function refresh(){
  if(polling)return;polling=true;
  try{
    const [state,list]=await Promise.all([api('chat/status'),api('chat/conversations')]);
    status=state;csrf=state.csrf;$('modelLabel').textContent=state.model||'Agent Runtime';$('connection').textContent=state.recovery_required?'待管理员恢复':state.busy?'协作进行中':'已连接';$('connection').className='connection';
    showHistory(list.conversations);
    const library=$('capabilityList'),nextLibrary=JSON.stringify(state.capabilities);if(nextLibrary!==librarySignature){librarySignature=nextLibrary;library.replaceChildren();for(const [title,rows] of [['工具',state.capabilities?.plugins],['Skill',state.capabilities?.skills]]){library.append(element('strong',title));for(const row of rows||[])library.append(element('p',row.id+'：'+row.description))}}
    if(selected){const target=selected,after=current?.id===target&&!current.turns.some(active)?current.revision:null;try{const chat=await api('chat/conversations/'+target+(after?'?after='+encodeURIComponent(after):''));if(target===selected&&!chat.not_modified)render(chat)}catch(error){if(error.status!==404)throw error;if(target===selected){current=null;$('welcome').hidden=true;$('messages').hidden=false;$('messages').replaceChildren(element('p','尚未找到会话记录。如刚才的请求状态不明确，请先到控制台核对，不要重复提交。','muted'))}}}
    controls();
  }catch(error){status=null;$('connection').textContent='连接中断';$('connection').className='connection offline';controls();notify(error.message)}
  finally{polling=false}
}
async function send(event){
  event.preventDefault();if($('send').disabled||sending)return;
  const prompt=$('prompt').value.trim()||(pendingAttachments.length?'请分析这些附件并回答。':'');if(!prompt)return;
  const target=selected||uuid();selected=target;address(target);sending=true;notify('');controls();
  const payload={conversation_id:target,request_id:uuid(),prompt,mode:$('mode').value,project:$('project').value,response_tokens:Number($('responseTokens').value),deep_thinking:$('deepThinking').checked,attachments:pendingAttachments.map(a=>a.id),
                 model_budget:{calls:Number($('modelCalls').value),output_tokens:Number($('modelTokens').value)}};
  try{await api('chat/turns',payload);if(selected===target){$('prompt').value='';pendingAttachments=[];showAttachments()}signature='';await refresh()}
  catch(error){notify(error.message+'。不会自动重复提交。');await refresh()}
  finally{sending=false;controls()}
}
function init(){
  const fromURL=new URLSearchParams(location.search).get('chat');let remembered=null;try{remembered=localStorage.getItem('agentrt-chat')}catch{}
  selected=/^[a-f0-9]{32}$/.test(fromURL||remembered||'')?(fromURL||remembered):null;
  $('newChat').onclick=newChat;$('chatForm').onsubmit=send;$('mode').onchange=controls;
  $('attachFiles').onclick=()=>$('fileInput').click();$('fileInput').onchange=uploadFiles;
  $('prompt').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();$('chatForm').requestSubmit()}};
  $('stop').onclick=async()=>{const turn=current?.turns.find(active);if(!turn?.job_id)return;$('stop').disabled=true;try{await api('jobs/'+turn.job_id+'/cancel',{});await refresh()}catch(error){notify(error.message)}finally{$('stop').disabled=false}};
  $('menu').onclick=()=>{const open=$('sidebar').classList.toggle('open');$('shade').hidden=!open};$('shade').onclick=closeMenu;
  document.onkeydown=event=>{if(event.key==='Escape'){closeMenu();$('budgetOptions').open=false}};
  for(const button of document.querySelectorAll('[data-prompt]'))button.onclick=()=>{$('mode').value=button.dataset.mode;$('prompt').value=button.dataset.prompt;controls();$('prompt').focus()};
  if(selected){$('welcome').hidden=true;$('messages').hidden=false;$('messages').append(element('p','正在读取对话…','muted'))}
  refresh();setInterval(refresh,1500);
}
init();
