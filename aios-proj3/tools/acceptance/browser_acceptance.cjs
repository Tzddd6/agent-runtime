const {spawn,spawnSync}=require('node:child_process');
const {once}=require('node:events');
const readline=require('node:readline');
const fs=require('node:fs/promises');
const path=require('node:path');
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
async function main(){
 const root=path.resolve(process.argv[3]||path.join(__dirname,'../..')), reports=path.resolve(process.env.AIOS_BROWSER_REPORTS||path.join(root,'reports/browser-'+Date.now()));await fs.mkdir(reports,{recursive:true});
 const python=process.argv[2]||process.env.AIOS_PYTHON||'python3';
 const fixtures=spawnSync(python,[path.join(__dirname,'make_upload_fixtures.py'),path.join(reports,'fixtures')],{cwd:root,windowsHide:true,encoding:'utf8'});assert.equal(fixtures.status,0,fixtures.stderr);
 const service=spawn(python,['-B','-m','tests.order_browser_fixture',path.join(reports,'server-data-'+Date.now())],{cwd:root,windowsHide:true,stdio:['ignore','pipe','inherit']});
 const closed=once(service,'close');let browser;
 const rows=[], errors=[];
 try{
  const [url]=await once(readline.createInterface({input:service.stdout}),'line',{signal:AbortSignal.timeout(15000)});
  browser=await chromium.launch({headless:true,...(process.env.AIOS_BROWSER_EXECUTABLE?{executablePath:process.env.AIOS_BROWSER_EXECUTABLE}:{})});
  const page=await browser.newPage({viewport:{width:1440,height:1000},acceptDownloads:true});page.on('pageerror',e=>errors.push(e.message));
  await page.goto(url+'console/');await page.selectOption('#preset','engineering');await page.selectOption('#projectSelect','order_project');
  const jobResponse=page.waitForResponse(r=>r.url().endsWith('/api/jobs')&&r.request().method()==='POST');
  await page.click('#submitJobBtn');const response=await jobResponse;assert.equal(response.status(),202);const {id}=await response.json();
  await page.waitForFunction(id=>document.querySelector('#engDownload')&&!document.querySelector('#engDownload').hidden&&document.querySelector('#engDownload').getAttribute('href')==='/api/jobs/'+id+'/artifact',id,{timeout:45000});
  assert.match(await page.locator('#engTestSummary').innerText(),/13/);assert.match(await page.locator('#engPhaseList').innerText(),/verify_round_2/);
  const detail=await page.evaluate(async id=>(await fetch('/api/jobs/'+id)).json(),id);
  assert.equal(detail.status,'succeeded');assert.ok(detail.results.baseline.failures>0);assert.equal(detail.results.answer.tests_run,13);
  const downloadEvent=page.waitForEvent('download');await page.click('#engDownload');const download=await downloadEvent;
  const zip=path.join(reports,'verified-order-project.zip');await download.saveAs(zip);
  const extracted=path.join(reports,'download-readback');const extract=spawnSync(python,['-m','zipfile','-e',zip,extracted],{windowsHide:true,encoding:'utf8'});assert.equal(extract.status,0,extract.stderr);
  const retest=spawnSync(python,['-B','-m','unittest','discover','-s','tests','-t','.','-v'],{cwd:extracted,windowsHide:true,encoding:'utf8'});assert.equal(retest.status,0,retest.stderr);assert.match(retest.stderr,/Ran 13 tests/);
  await fs.writeFile(path.join(reports,'download-unittest.log'),retest.stderr);
  assert.match(await page.locator('#workerEvidence').innerText(),/PID/);assert.match(await page.locator('#engPhaseList').innerText(),/tests_failed/);assert.match(await page.locator('#dagTaskTableWrap').innerText(),/ctx_/);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+2));
  await page.screenshot({path:path.join(reports,'order-completed.png'),fullPage:true});rows.push({case:'browser_order_two_round_download_retest',passed:true,job:id});
  await page.reload();await page.waitForFunction(()=>document.querySelector('#engDownload')&&!document.querySelector('#engDownload').hidden,{},{timeout:15000});rows.push({case:'refresh_restores_completed_job',passed:true});
  await page.selectOption('#preset','engineering');await page.fill('#modelCallsBudget','0');await page.fill('#modelTokensBudget','0');
  const blockedResponse=page.waitForResponse(r=>r.url().endsWith('/api/jobs')&&r.request().method()==='POST');await page.click('#submitJobBtn');const blocked=await blockedResponse;assert.equal(blocked.status(),202);const blockedId=(await blocked.json()).id;
  let failed;const deadline=Date.now()+15000;
  do { failed=await page.evaluate(async id=>(await fetch('/api/jobs/'+id)).json(),blockedId);if(failed.status==='failed'&&failed.model_budget)break;await new Promise(r=>setTimeout(r,25)); } while(Date.now()<deadline);
  assert.equal(failed.status,'failed');assert.equal(failed.model_budget.calls,0);assert.ok(!failed.results.answer);rows.push({case:'zero_budget_no_provider_no_artifact',passed:true});
  await page.goto(url);await page.locator('#chatForm input[type=file]').setInputFiles({name:'broken.png',mimeType:'image/png',buffer:Buffer.from('not an image')});
  await page.waitForFunction(()=>document.body.textContent.includes('Invalid')||document.body.textContent.includes('上传失败')||document.body.textContent.includes('无效'),{},{timeout:10000});rows.push({case:'invalid_image_visible_failure',passed:true});
  await page.locator('#chatForm input[type=file]').setInputFiles(['valid.png','valid.pdf','valid.docx','fixture.txt'].map(name=>path.join(reports,'fixtures',name)));
  await page.getByRole('status').filter({hasText:'已上传 4 个附件'}).waitFor();
  rows.push({case:'browser_valid_png_pdf_docx_text_uploads',passed:true});
  await page.fill('#chatPrompt','<img src=x onerror="window.injected=true"> format test');await page.click('#sendChatBtn');
  await page.waitForFunction(()=>document.querySelector('#sendChatBtn').disabled&&document.querySelector('#messagesBox').textContent.includes('粗体'));
  await page.waitForFunction(()=>!document.querySelector('#sendChatBtn').disabled);
  const answer=page.locator('#messagesBox article').last();assert.equal(await answer.locator('table').count(),1);assert.equal(await answer.locator('pre code').count(),1);assert.ok(await answer.locator('.katex').count()>0);assert.equal(await page.evaluate(()=>window.injected),undefined);
  await page.screenshot({path:path.join(reports,'chat-formats.png'),fullPage:true});rows.push({case:'attachment_stream_math_code_table_long_xss',passed:true});assert.deepEqual(errors,[]);
 }finally{if(browser)await browser.close();service.kill();await closed;await fs.writeFile(path.join(reports,'browser-results.json'),JSON.stringify({scope:'offline fixed model; real browser, HTTP, Worker and unittest',rows,errors},null,2));}
 console.log(JSON.stringify({passed:rows.length,rows,errors}));
}
main().catch(e=>{console.error(e);process.exitCode=1});







