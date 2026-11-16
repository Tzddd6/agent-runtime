/* A bounded Markdown subset built from DOM nodes. Raw HTML is always text. */
(function(root){
  'use strict';
  function renderMarkdown(target,source,copyCode){
    const doc=target.ownerDocument,make=(tag,text)=>{const n=doc.createElement(tag);if(text!==undefined)n.textContent=text;return n};
    const literal=(parent,text)=>parent.append(doc.createTextNode(text));
    function math(parent,value,display,original){
      const node=make(display?'div':'span');node.className=display?'math-block':'math-inline';
      if(root.katex&&value.length<=8000){try{root.katex.render(value,node,{displayMode:display,throwOnError:false,trust:false,maxExpand:500,maxSize:10,strict:'ignore',macros:{}})}catch{node.textContent=original}}
      else node.textContent=original;
      parent.append(node);
    }
    function closing(text,mark,start){let i=start;while((i=text.indexOf(mark,i))>=0){let slashes=0;for(let j=i-1;j>=0&&text[j]==='\\';j--)slashes++;if(slashes%2===0)return i;i+=mark.length}return -1}
    function inline(parent,text,depth=0){
      if(depth>8){literal(parent,text);return}
      let i=0,buffer='';const flush=()=>{if(buffer){literal(parent,buffer);buffer=''}};
      while(i<text.length){
        if(text[i]==='`'){
          const mark=text.slice(i).match(/^`+/)[0],end=text.indexOf(mark,i+mark.length);
          if(end>=0){flush();parent.append(make('code',text.slice(i+mark.length,end)));i=end+mark.length;continue}
        }
        let formula=false;
        for(const [left,right,display] of [['\\(','\\)',false],['\\[','\\]',true],['$$','$$',true],['$','$',false]]){
          if(!text.startsWith(left,i))continue;
          const end=closing(text,right,i+left.length);
          if(end>i+left.length&&(left!=='$'||!(/\s/.test(text[i+1])||/\s/.test(text[end-1])))){
            flush();math(parent,text.slice(i+left.length,end),display,text.slice(i,end+right.length));i=end+right.length;formula=true;break;
          }
          if(left.startsWith('\\')){buffer+=left;i+=left.length;formula=true;break}
        }
        if(formula)continue;
        if(text[i]==='\\'&&/[\\`*_{}[\]()#+.!|>~-]/.test(text[i+1]||'')){buffer+=text[i+1];i+=2;continue}
        const image=text[i]==='!'&&text[i+1]==='[';
        if(image||text[i]==='['){
          const start=i+(image?1:0),middle=closing(text,'](',start+1),end=middle>=0?closing(text,')',middle+2):-1;
          if(end>=0){
            const raw=text.slice(i,end+1),label=text.slice(start+1,middle),url=text.slice(middle+2,end).trim();
            let safe=false;try{safe=['https:','http:'].includes(new URL(url).protocol)&&!/[\s\u0000-\u001f]/.test(url)}catch{}
            if(!image&&safe){flush();const a=make('a');a.href=url;a.target='_blank';a.rel='noopener noreferrer';inline(a,label,depth+1);parent.append(a)}
            else buffer+=raw;
            i=end+1;continue;
          }
        }
        let matched=false;
        for(const mark of ['***','___','**','__','~~','*','_']){
          if(!text.startsWith(mark,i))continue;
          if(mark.includes('_')&&/[\p{L}\p{N}]/u.test(text[i-1]||'')&&/[\p{L}\p{N}]/u.test(text[i+mark.length]||''))continue;
          const end=closing(text,mark,i+mark.length);if(end<0||end===i+mark.length)continue;
          flush();const n=make(mark==='~~'?'del':mark.length===1?'em':'strong');
          if(mark.length===3){const em=make('em');inline(em,text.slice(i+3,end),depth+1);n.append(em)}
          else inline(n,text.slice(i+mark.length,end),depth+1);
          parent.append(n);i=end+mark.length;matched=true;break;
        }
        if(matched)continue;buffer+=text[i++];
      }
      flush();
    }
    const listItem=line=>{const m=line.match(/^( *)([-+*]|\d+[.)])\s+(.*)$/);return m?{indent:m[1].length,ordered:/^\d/.test(m[2]),number:parseInt(m[2],10),body:m[3]}:null};
    const fence=line=>line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    const heading=line=>line.match(/^ {0,3}(#{1,6})\s+(.+?)\s*$/);
    const rule=line=>/^ {0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$/.test(line);
    function cells(line){
      let text=line.trim();if(text.startsWith('|'))text=text.slice(1);if(text.endsWith('|')&&!text.endsWith('\\|'))text=text.slice(0,-1);
      const out=[];let value='',ticks=0;
      for(let i=0;i<text.length;i++){const c=text[i];if(c==='\\'&&text[i+1]==='|'){value+='|';i++;continue}if(c==='`')ticks^=1;if(c==='|'&&!ticks){out.push(value.trim());value=''}else value+=c}out.push(value.trim());return out;
    }
    const divider=line=>{const a=cells(line);return a.length>1&&a.every(c=>/^:?-{3,}:?$/.test(c))};
    function blocks(parent,lines,depth=0){
      if(depth>8){const p=make('p',lines.join('\n'));parent.append(p);return}
      let i=0;
      while(i<lines.length){
        if(!lines[i].trim()){i++;continue}
        const f=fence(lines[i]);
        if(f){
          const mark=f[1],code=[],lang=f[2].trim().slice(0,32)||'代码';i++;
          const close=new RegExp('^ {0,3}'+(mark[0]==='`'?'`':'~')+'{'+mark.length+',}\\s*$');
          while(i<lines.length&&!close.test(lines[i]))code.push(lines[i++]);const closed=i<lines.length;if(closed)i++;
          const value=code.join('\n')+(closed&&code.length?'\n':''),box=make('div'),head=make('div'),pre=make('pre'),button=make('button','复制');
          box.className='code-block';head.className='code-head';button.className='small-button';button.type='button';
          button.onclick=()=>copyCode&&copyCode(value,button);head.append(make('span',lang),button);pre.append(make('code',value));box.append(head,pre);parent.append(box);continue;
        }
        const begin=lines[i].match(/^ {0,3}(\\\[|\$\$)/);
        if(begin){
          const left=begin[1],right=left==='\\['?'\\]':'$$',raw=lines.slice(i).join('\n'),start=raw.indexOf(left),end=closing(raw,right,start+left.length);
          if(end>=start+left.length){
            math(parent,raw.slice(start+left.length,end),true,raw.slice(start,end+right.length));
            const consumed=raw.slice(0,end+right.length).split('\n').length,tail=raw.slice(end+right.length).split('\n')[0];
            i+=consumed;if(tail.trim()){lines.splice(i,0,tail)}continue;
          }
        }
        const h=heading(lines[i]);if(h){const n=make('h'+h[1].length);inline(n,h[2].replace(/\s+#+\s*$/,''));parent.append(n);i++;continue}
        if(rule(lines[i])){parent.append(make('hr'));i++;continue}
        if(/^ {0,3}>/.test(lines[i])){
          const quote=[];while(i<lines.length&&/^ {0,3}>/.test(lines[i]))quote.push(lines[i++].replace(/^ {0,3}> ?/,''));
          const n=make('blockquote');blocks(n,quote,depth+1);parent.append(n);continue;
        }
        if(i+1<lines.length&&lines[i].includes('|')&&divider(lines[i+1])){
          const headers=cells(lines[i]),wrap=make('div'),table=make('table'),head=make('thead'),row=make('tr'),body=make('tbody');wrap.className='markdown-table';
          for(const value of headers){const th=make('th');inline(th,value);row.append(th)}head.append(row);i+=2;
          while(i<lines.length&&lines[i].trim()&&lines[i].includes('|')){const values=cells(lines[i++]),tr=make('tr');headers.forEach((_,c)=>{const td=make('td');inline(td,values[c]||'');tr.append(td)});body.append(tr)}
          table.append(head,body);wrap.append(table);parent.append(wrap);continue;
        }
        const first=listItem(lines[i]);
        if(first){
          const list=make(first.ordered?'ol':'ul');if(first.ordered&&first.number!==1)list.start=first.number;
          while(i<lines.length){
            const item=listItem(lines[i]);if(!item||item.indent!==first.indent||item.ordered!==first.ordered)break;
            const content=[item.body];i++;
            while(i<lines.length){
              const next=listItem(lines[i]);if(next&&next.indent<=first.indent)break;
              if(!lines[i].trim()){
                let peek=i+1;while(peek<lines.length&&!lines[peek].trim())peek++;
                if(peek===lines.length||lines[peek].search(/\S/)<first.indent+2)break;
              }else if(lines[i].search(/\S/)<first.indent+2)break;
              const continuation=lines[i++],indent=continuation.search(/\S/);
              content.push(continuation.slice(Math.min(first.indent+2,indent<0?first.indent+2:indent)));
            }
            const li=make('li');blocks(li,content,depth+1);list.append(li);
            while(i<lines.length&&!lines[i].trim())i++;
          }
          parent.append(list);continue;
        }
        const paragraph=[];
        while(i<lines.length&&lines[i].trim()){
          if(paragraph.length&&(fence(lines[i])||heading(lines[i])||rule(lines[i])||listItem(lines[i])||/^ {0,3}(?:>|\\\[|\$\$)/.test(lines[i])||i+1<lines.length&&divider(lines[i+1])))break;
          paragraph.push(lines[i++].replace(/ {2,}$/,''));
        }
        const p=make('p');paragraph.forEach((line,index)=>{if(index)p.append(make('br'));inline(p,line)});parent.append(p);
      }
    }
    blocks(target,String(source).replace(/\r\n?/g,'\n').split('\n'));
  }
  if(typeof module!=='undefined'&&module.exports)module.exports={renderMarkdown};
  else root.renderMarkdown=renderMarkdown;
})(typeof window!=='undefined'?window:globalThis);
