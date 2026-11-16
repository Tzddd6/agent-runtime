'use strict';
let libraryTab='plugins',knowledgeEdit=null,knowledgeBusy=false,knowledgeSearchSequence=0;
const titles={general:'常规与额度',appearance:'外观',permissions:'权限与安全',knowledge:'知识库',capabilities:'工具与 Skill',storage:'数据与存储'};
const permissionNames={sandboxed_command:'隔离命令',public_network:'公网读取',workspace_write:'工作目录写入',runtime_control:'Runtime 控制',bounded_read_or_compute:'限量读取 / 计算'};
function panelNotice(text){$('workspaceNotice').textContent=text;$('workspaceNotice').hidden=!text}
async function openWorkspace(panel){
  if(!(panel in titles))return;closeMenu();panelNotice('');$('workspaceTitle').textContent=titles[panel];
  for(const section of document.querySelectorAll('[data-settings]'))section.hidden=section.dataset.settings!==panel;
  for(const button of document.querySelectorAll('.settings-nav [data-panel]')){const chosen=button.dataset.panel===panel;button.classList.toggle('active',chosen);button.setAttribute('aria-current',String(chosen))}
  if(!$('workspaceDialog').open)$('workspaceDialog').showModal();
  try{if(panel==='knowledge')await loadKnowledge();if(panel==='storage')await loadStorage();if(panel==='capabilities')renderLibrary()}catch(error){panelNotice(error.message)}
}
function renderLibrary(){
  const query=$('capabilityQuery').value.trim().toLowerCase(),box=$('capabilityList');box.replaceChildren();
  const rows=status?.capabilities?.[libraryTab]||[];
  for(const row of rows.filter(r=>(r.id+' '+r.description).toLowerCase().includes(query))){
    const card=element('article',null,'library-card'),top=element('div',null,'library-card-top');
    top.append(element('span',libraryTab==='skills'?'SKILL':'TOOL','library-type'),element('span','已注册','registered-label'));
    card.append(top,element('h3',row.id),element('p',row.description));
    if(row.permissions){const tags=element('div',null,'permission-tags');for(const p of row.permissions)tags.append(element('span',permissionNames[p]||p));card.append(tags)}
    else card.append(element('small','按任务加载 · 不增加操作权限','muted'));
    box.append(card);
  }
  if(!box.childElementCount)box.append(element('p',status?'没有匹配的能力。':'正在连接服务…','empty-state'));
}
async function loadKnowledge(){
  const value=await api('knowledge'),box=$('knowledgeList');box.replaceChildren();
  $('knowledgeStats').textContent=value.documents.length+' 篇 · '+value.chunks+' 个片段';$('knowledgeCount').textContent=value.documents.length;
  if(!value.documents.length)box.append(element('div','资料库还是空的。导入项目说明或新建笔记，下一次对话即可检索。','empty-state'));
  for(const doc of value.documents){
    const row=element('article',null,'knowledge-row'),icon=element('span','▤','document-icon'),info=element('div',null,'document-info');
    info.append(element('b',doc.title),element('small','v'+doc.revision+' · '+Math.ceil(doc.size/1024)+' KiB · '+new Date(doc.updated*1000).toLocaleDateString('zh-CN')));
    const edit=element('button','查看 / 编辑','outlined-button');edit.type='button';edit.onclick=async()=>{if(knowledgeBusy)return;try{const data=await api('knowledge/'+doc.id);knowledgeEdit=data;$('knowledgeTitle').value=data.title;$('knowledgeBody').value=data.body;showKnowledgeEditor('编辑资料 · v'+data.revision)}catch(error){panelNotice(error.message)}};
    const remove=element('button','移除','text-button danger');remove.type='button';let armed=false;
    remove.onclick=async()=>{if(knowledgeBusy)return;if(!armed){armed=true;remove.textContent='确认移除';setTimeout(()=>{armed=false;remove.textContent='移除'},4000);return}
      setKnowledgeBusy(true);try{await api('knowledge/remove',{id:doc.id,expected_sha256:doc.sha256});if(knowledgeEdit?.id===doc.id)closeKnowledgeEditor();await loadKnowledge();resetKnowledgeSearch();panelNotice('资料已移除，历史引用快照保留。')}catch(error){panelNotice(error.message)}finally{setKnowledgeBusy(false)}};
    row.append(icon,info,edit,remove);box.append(row);
  }
}
function setKnowledgeBusy(value){knowledgeBusy=value;for(const button of document.querySelectorAll('[data-settings="knowledge"] button'))button.disabled=value}
function showKnowledgeEditor(title){$('knowledgeEditorTitle').textContent=title;$('knowledgeEditor').hidden=false;$('knowledgeEditor').scrollIntoView({block:'nearest',behavior:'smooth'});$('knowledgeTitle').focus()}
function closeKnowledgeEditor(){knowledgeEdit=null;$('knowledgeEditor').hidden=true;$('knowledgeEditor').reset()}
function newKnowledgeNote(){if(knowledgeBusy)return;closeKnowledgeEditor();showKnowledgeEditor('新建资料')}
async function saveKnowledge(event){
  event.preventDefault();if(knowledgeBusy)return;
  // Retain a single identity on ambiguous acknowledgement; no automatic POST replay.
  if(!knowledgeEdit)knowledgeEdit={id:uuid()};
  const payload={id:knowledgeEdit.id,title:$('knowledgeTitle').value.trim(),body:$('knowledgeBody').value};
  if(knowledgeEdit.sha256)payload.expected_sha256=knowledgeEdit.sha256;
  if(new TextEncoder().encode(payload.body).length>256*1024){panelNotice('单篇资料最多256KiB。');return}
  setKnowledgeBusy(true);panelNotice('正在保存并建立本地索引…');
  try{await api('knowledge/save',payload);closeKnowledgeEditor();await loadKnowledge();resetKnowledgeSearch();panelNotice('已保存。启用知识库的下一次对话会自动检索。')}
  catch(error){panelNotice(error.message+'。不会自动重试；可先刷新列表核对。')}
  finally{setKnowledgeBusy(false)}
}
function resetKnowledgeSearch(){++knowledgeSearchSequence;$('knowledgeQuery').value='';$('knowledgeResults').replaceChildren();$('knowledgeResults').hidden=true}
async function searchKnowledge(){
  const sequence=++knowledgeSearchSequence,query=$('knowledgeQuery').value.trim(),box=$('knowledgeResults');
  box.replaceChildren();box.hidden=!query;if(!query)return;
  try{const value=await api('knowledge/search?q='+encodeURIComponent(query));if(sequence!==knowledgeSearchSequence)return;
    if(!value.sources.length)box.append(element('p','没有命中相关片段。试试资料中出现的关键词。','empty-state'));
    for(const source of value.sources){const card=element('article',null,'search-result');card.append(element('b',source.title+' · v'+source.revision+' · 第'+source.line_start+'—'+source.line_end+'行'),element('p',source.body));box.append(card)}
  }catch(error){if(sequence===knowledgeSearchSequence)panelNotice(error.message)}
}
function knowledgeSources(turn){
  const knowledge=turn.knowledge,sources=knowledge.sources||[],view=element('details',null,'knowledge-sources');
  view.append(element('summary',sources.length?'▤ 检索到 '+sources.length+' 个资料片段 · '+knowledge.search_ms+' ms':'▤ 本次未命中知识库资料'));
  for(const source of sources){const card=element('article',null,'source-card');card.id='source-'+turn.request_id+'-'+source.citation;
    const cited=new RegExp('\\['+source.citation+'\\]').test(turn.answer||'');
    card.append(element('b','['+source.citation+'] '+source.title),element('small','v'+source.revision+' · 第'+source.line_start+'—'+source.line_end+'行 · '+(cited?'回答包含此标记':'检索候选，回答未标记引用')),element('p',source.body),element('code','SHA256 '+source.sha256.slice(0,16)+'…'));view.append(card)}
  if(sources.length)view.append(element('p','此处保存的是本次检索快照。引用标记并不自动证明结论正确，请核对原文。','setting-footnote'));
  return view;
}
function linkKnowledgeCitations(body,turn){
  const known=new Set((turn.knowledge?.sources||[]).map(s=>s.citation)),walker=document.createTreeWalker(body,NodeFilter.SHOW_TEXT),nodes=[];
  while(walker.nextNode())if(!walker.currentNode.parentElement.closest('code,pre,a'))nodes.push(walker.currentNode);
  for(const text of nodes){const matches=[...text.textContent.matchAll(/\[(K[1-6])\]/g)].filter(m=>known.has(m[1]));if(!matches.length)continue;
    const fragment=document.createDocumentFragment();let offset=0;
    for(const match of matches){fragment.append(document.createTextNode(text.textContent.slice(offset,match.index)));const link=element('a',match[0],'citation-link');const target='source-'+turn.request_id+'-'+match[1];link.href='#'+target;link.title='核对知识库来源 '+match[1];link.onclick=event=>{event.preventDefault();const card=document.getElementById(target);if(card){card.closest('details').open=true;card.scrollIntoView({block:'center',behavior:'smooth'})}};fragment.append(link);offset=match.index+match[0].length}
    fragment.append(document.createTextNode(text.textContent.slice(offset)));text.replaceWith(fragment);
  }
}
async function loadStorage(){
  const [db,kb]=await Promise.all([api('database/status'),api('knowledge')]),box=$('storageStats');box.replaceChildren();
  for(const [label,value] of [['附件',db.attachments],['知识文档',kb.documents.length],['知识片段',kb.chunks],['工具审计',db.audit_events],['资源采样',db.observations]]){const card=element('div',null,'storage-stat');card.append(element('span',label),element('b',value));box.append(card)}
}
function applyAppearance(){
  const choice=$('themeSelect').value,dark=choice==='dark'||choice==='system'&&matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme=dark?'dark':'light';document.documentElement.style.setProperty('--answer-size',$('fontSelect').value+'px');
}
function savePreferences(){
  const value={theme:$('themeSelect').value,font:$('fontSelect').value,knowledge:$('useKnowledge').checked,
    calls:$('modelCalls').value,tokens:$('modelTokens').value,response:$('responseTokens').value,context:$('contextMode').value};
  try{localStorage.setItem('agentrt-preferences-v1',JSON.stringify(value))}catch{}applyAppearance();
}
function initWorkbench(){
  for(const button of document.querySelectorAll('[data-panel]'))button.onclick=()=>openWorkspace(button.dataset.panel);
  $('closeWorkspace').onclick=()=>$('workspaceDialog').close();$('workspaceDialog').onclick=event=>{if(event.target===$('workspaceDialog'))$('workspaceDialog').close()};
  $('newKnowledge').onclick=newKnowledgeNote;$('cancelKnowledge').onclick=closeKnowledgeEditor;$('knowledgeEditor').onsubmit=saveKnowledge;
  $('importKnowledge').onclick=()=>$('knowledgeInput').click();$('knowledgeInput').onchange=async()=>{const file=$('knowledgeInput').files?.[0];$('knowledgeInput').value='';if(!file||knowledgeBusy)return;
    try{if(file.size>256*1024||!/^.+\.(txt|md|py|json|csv|log)$/i.test(file.name))throw Error('仅支持256KiB以内的UTF-8文本文件');const text=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());newKnowledgeNote();$('knowledgeTitle').value=file.name.slice(0,120);$('knowledgeBody').value=text;panelNotice('已读取文件，确认内容后点击“保存并建立索引”。')}catch(error){panelNotice(error.message)}};
  let searchTimer;$('knowledgeQuery').oninput=()=>{clearTimeout(searchTimer);++knowledgeSearchSequence;searchTimer=setTimeout(searchKnowledge,250)};
  $('capabilityQuery').oninput=renderLibrary;for(const button of document.querySelectorAll('[data-library]'))button.onclick=()=>{libraryTab=button.dataset.library;for(const b of document.querySelectorAll('[data-library]'))b.classList.toggle('active',b===button);renderLibrary()};
  try{const p=JSON.parse(localStorage.getItem('agentrt-preferences-v1')||'{}');if(['light','dark','system'].includes(p.theme))$('themeSelect').value=p.theme;if(['14','16','18'].includes(p.font))$('fontSelect').value=p.font;if(typeof p.knowledge==='boolean')$('useKnowledge').checked=p.knowledge;for(const [key,id,max,min] of [['calls','modelCalls',128,0],['tokens','modelTokens',262144,0],['response','responseTokens',4096,256]])if(/^\d+$/.test(p[key])&&+p[key]<=max&&+p[key]>=min)$(id).value=p[key]}catch{}
  try{const p=JSON.parse(localStorage.getItem('agentrt-preferences-v1')||'{}');if(['balanced','full'].includes(p.context))$('contextMode').value=p.context}catch{}
  for(const id of ['themeSelect','fontSelect','useKnowledge','modelCalls','modelTokens','responseTokens','contextMode'])$(id).addEventListener('change',savePreferences);applyAppearance();
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change',applyAppearance);
  document.addEventListener('keydown',event=>{if(event.altKey&&event.key.toLowerCase()==='n'&&!$('workspaceDialog').open){event.preventDefault();newChat()}});
}
initWorkbench();
