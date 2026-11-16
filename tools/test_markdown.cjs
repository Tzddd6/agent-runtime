/* DOM contract tests. Browser validation separately checks real layout. */
const assert=require('node:assert/strict');
const {renderMarkdown}=require('../agent_runtime/static/markdown.js');
class Node{
  constructor(tag,doc,text=''){this.tagName=tag;this.ownerDocument=doc;this.children=[];this.value=text}
  append(...nodes){for(const n of nodes)this.children.push(typeof n==='string'?new Node('#text',this.ownerDocument,n):n)}
  set textContent(value){this.children=[];this.value=String(value)}
  get textContent(){return this.value+this.children.map(n=>n.textContent).join('')}
}
const doc={createElement(tag){return new Node(tag,doc)},createTextNode(text){return new Node('#text',doc,text)}};
function render(text,copy){const box=doc.createElement('div');renderMarkdown(box,text,copy);return box}
function all(node,tag){return [node,...node.children.flatMap(c=>all(c,tag))].filter(n=>n.tagName===tag)}
let cases=0;
function test(name,fn){fn();cases++;process.stdout.write('OK '+name+'\n')}
test('Chinese headings, bold with author trailing space, paragraphs and line breaks',()=>{
  const box=render('## 计算机系统的组成\n\n常见划分是两大块：\n### （1）硬件\n\n- **CPU（中央处理器） **：运算和控制核心\n- **内存**：暂存数据\n\n硬件提供**计算能力**。\n下一行。');
  assert.equal(all(box,'h2')[0].textContent,'计算机系统的组成');
  assert.equal(all(box,'h3')[0].textContent,'（1）硬件');
  assert.deepEqual(all(box,'strong').map(n=>n.textContent),['CPU（中央处理器） ','内存','计算能力']);
  assert.equal(all(box,'li').length,2);assert.equal(all(box,'br').length,1);
  assert(!box.textContent.includes('**'));assert(!box.textContent.includes('###'));
  assert.equal(all(render('### C#'), 'h3')[0].textContent,'C#');
});
test('Nested ordered and unordered lists keep hierarchy',()=>{
  const box=render('3. **系统软件**\n   - 操作系统\n   - 编译器\n4. **应用软件**\n   - 浏览器\n\n普通段落');
  assert.equal(all(box,'ol')[0].start,3);assert.equal(all(box,'ul').length,2);
  assert.equal(all(box,'ol')[0].children.length,2);assert.equal(all(box,'li').length,5);
  assert.equal(box.children.at(-1).textContent,'普通段落');
});
test('Fenced code retains indentation, tabs, Markdown and internal backticks',()=>{
  let copied;
  const code='\tprint("**CPU**")\nconst text = "```inside";\n';
  const box=render('代码：\n````python\n'+code+'````\n\n后文',v=>copied=v);
  assert.equal(all(box,'pre')[0].textContent,code);assert.equal(all(box,'strong').length,0);
  all(box,'button')[0].onclick();assert.equal(copied,code);
});
test('Inline code, escaped Markdown, emphasis and intraword underscores',()=>{
  const box=render('`**原样**` 和 \\*\\*字面\\*\\*，*斜体*，foo_bar_baz，~~删除~~。');
  assert.equal(all(box,'code')[0].textContent,'**原样**');assert.equal(all(box,'strong').length,0);
  assert.equal(all(box,'em').length,1);assert.equal(all(box,'del').length,1);
  assert(box.textContent.includes('foo_bar_baz'));
});
test('Quotes, separators and a table with an escaped pipe',()=>{
  const box=render('> **注意**\n> 第二行\n\n---\n\n| 项目 | 值 |\n| --- | --- |\n| A\\|B | **1** |');
  assert.equal(all(box,'blockquote').length,1);assert.equal(all(box,'hr').length,1);
  assert.equal(all(box,'table').length,1);assert.equal(all(box,'th').length,2);
  assert.equal(all(box,'td')[0].textContent,'A|B');assert.equal(all(box,'td')[1].textContent,'1');
});
test('Raw HTML, event handlers, remote images and executable links stay inert',()=>{
  const box=render('<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n\n[危险](javascript:alert) ![图](https://example.com/key) [安全](https://example.com/docs)');
  assert.equal(all(box,'script').length,0);assert.equal(all(box,'img').length,0);
  assert.equal(all(box,'a').length,1);assert.equal(all(box,'a')[0].href,'https://example.com/docs');
  assert.equal(all(box,'a')[0].rel,'noopener noreferrer');assert(box.textContent.includes('<script>'));
});
test('Malformed and unterminated syntax is visible and bounded',()=>{
  const box=render('**未结束\n\n```python\nx = "**literal**"');
  assert(box.textContent.includes('**未结束'));assert.equal(all(box,'pre')[0].textContent,'x = "**literal**"');
  render('> '.repeat(100)+'x');render('*'.repeat(8000));
});
test('Math is preserved before Markdown escapes and code never renders math',()=>{
  const seen=[];global.katex={render(value,node,options){seen.push({value,options});node.textContent='MATH'}};
  const box=render('公式 \\(f(t)\\)\n\n\\[\nF(s)=\\int_0^\\infty f(t)e^{-st}dt\n\\]\n\n$x_1$ 和 **加粗**\n\n```latex\n\\[code\\]\n```');
  assert.equal(seen.length,3);assert.equal(seen[0].value,'f(t)');assert(seen[1].value.includes('\\int_0'));
  assert(seen[1].options.displayMode);assert.equal(seen[0].options.trust,false);assert.equal(seen[0].options.maxExpand,500);
  assert.equal(all(box,'pre')[0].textContent,'\\[code\\]\n');assert.equal(all(box,'strong')[0].textContent,'加粗');
  const partial=render('暂未闭合 \\(x_1');assert(partial.textContent.includes('\\(x_1'));
  delete global.katex;
});
process.stdout.write(JSON.stringify({tests:cases,passed:cases,paid_model_calls:0})+'\n');
