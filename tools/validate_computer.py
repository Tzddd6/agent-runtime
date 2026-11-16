"""Verify the dedicated browser sandbox; no supplier requests or host secrets."""
import json,sys,tempfile,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_runtime.computer_worker import browser_wrapper,browser_executable

def main():
    from playwright.sync_api import sync_playwright
    ready=Path.home()/'.local/share/agentrt-browser-ready.json'
    ready.unlink(missing_ok=True)
    report={};canary=Path.home()/('.agentrt-synthetic-'+uuid.uuid4().hex)
    canary.write_text('SYNTHETIC-ISOLATION-CANARY')
    try:
        with tempfile.TemporaryDirectory() as folder,sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path=browser_wrapper(Path(folder),browser_executable(pw)),headless=False,chromium_sandbox=True,timeout=20000,
                args=['--disable-setuid-sandbox','--disable-gpu','--renderer-process-limit=2','--disable-dev-shm-usage'])
            try:
                context=browser.new_context();page=context.new_page()
                page.set_content('<button onclick="this.textContent=\'OK\'">Test</button>');page.get_by_role('button').click()
                assert page.get_by_role('button').inner_text()=='OK';report['actual_click']=True
                report['browser_version']=browser.version;page.close()
                for name,url in [('host_home_blocked',canary.as_uri()),('host_loopback_blocked','http://127.0.0.1:18880/')]:
                    negative=context.new_page()
                    try:negative.goto(url,timeout=3000);report[name]=False
                    except Exception:report[name]=True
                    finally:negative.close()
                assert report['host_home_blocked'] and report['host_loopback_blocked']
                page=context.new_page();page.goto('chrome://sandbox');status=page.locator('body').inner_text()
                assert 'adequately sandboxed' in status.lower()
                report['chromium_sandbox_verified']=True
            finally:browser.close()
        ready.parent.mkdir(parents=True,exist_ok=True)
        ready.write_text(json.dumps({'verified':True,'version':report['browser_version'],'isolation':report}));ready.chmod(0o600)
        print(json.dumps(report));return 0
    finally:canary.unlink(missing_ok=True)

if __name__=='__main__':raise SystemExit(main())
