"""Independent functional follow-up of conservative default; not a new A/B win."""
import hashlib,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.benchmark_execution_quality import child,run_study
if len(sys.argv)>1 and sys.argv[1]=='--child':
    child(sys.argv[2],int(sys.argv[4]),sys.argv[6],sys.argv[8])
else:
    folder=(ROOT/'reports/full-repair-followup-20261116').resolve();folder.mkdir(mode=0o700,exist_ok=False)
    original=ROOT/'reports/engineering-v18-frozen-20261116/dataset.json'
    data=json.loads(original.read_text())
    for case in data['cases']:case['order']=['runtime_fork_full']
    (folder/'dataset.json').write_text(json.dumps(data,ensure_ascii=False,indent=2))
    # run_study launches its own module entry point, which supports this arm.
    runner=ROOT/'tools/benchmark_execution_quality.py'
    protocol={'purpose':'independent acceptance of full-source default after disabling compact default',
        'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'runner_sha256':hashlib.sha256(runner.read_bytes()).hexdigest(),
        'dataset_sha256':hashlib.sha256((folder/'dataset.json').read_bytes()).hexdigest(),
        'global_max_paid_intents':16,'cases':8,'max_calls_per_case':2,'prior_dataset_sha256':hashlib.sha256(original.read_bytes()).hexdigest(),
        'limits':['not a paired rerun of every arm, no performance superiority claim','all original failures retained']}
    (folder/'protocol.json').write_text(json.dumps(protocol,indent=2))
    run_study(str(folder))
