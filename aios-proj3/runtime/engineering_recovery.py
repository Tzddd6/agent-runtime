"""Evidence and conservative recovery checks for controlled engineering workflows."""
from hashlib import sha256
import json

from aios.tool import engineering


class ReconciliationRequired(ValueError):
    pass


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def input_version(project, manifest):
    spec = engineering.PROJECTS[project]
    tests = spec['tests'] if spec.get('multi_file') else (spec['public'], spec['heldout'])
    return digest({'manifest': manifest, 'tests': tests})


def candidate_hash(project, patch):
    if engineering.PROJECTS[project].get('multi_file'):
        files = engineering.validate_multifile_patch(patch['files'], project)
        return engineering.compute_tree_sha256(files)
    source = engineering.validate_patch(patch['source'], project)
    return sha256(source.encode()).hexdigest()


def interrupted_worker(saved, workflow):
    if not saved:
        return None
    states = saved['states']
    if states.get('patch') != 'succeeded' or states.get('verify') != 'interrupted':
        return None
    if any(state == 'running' or (state == 'interrupted' and task != 'verify')
           for task, state in states.items()):
        return None
    if any(states.get(task) != 'waiting' for task in ('review', 'answer')):
        return None
    for event in reversed(saved['events']):
        if event.get('task_id') != 'verify':
            continue
        worker = event.get('worker') or {}
        if (event['state'] == 'interrupted' and worker.get('operation') == 'project.patch_check'
                and worker.get('agent') == f'{workflow}-tester_agent' and worker.get('reclaimed') is True
                and worker.get('interrupted_before_start') is True and worker.get('returncode')):
            return worker
        break
    return None


def verify_resume(kernel, workflow, project, version, saved):
    if not interrupted_worker(saved, workflow):
        raise ReconciliationRequired('未确认操作需核对后处理，工作流已暂停。')
    try:
        candidate = saved['results']['patch']
        receipt = candidate['model_receipt']
        refs = saved['context_refs']
        completed_events = {event['task_id']: event for event in saved['events']
                            if event.get('state') == 'succeeded'}
        for task, state in saved['states'].items():
            if state == 'succeeded':
                expected = digest(saved['results'][task])
                if completed_events[task].get('result_sha256') != expected or refs[task] != 'ctx_' + expected:
                    raise ValueError('Completed node digest mismatch')
        patch_ref = refs['patch']
        completed = kernel.context_store.get(patch_ref, f'{workflow}-tester_agent')
        raw = kernel.context_store.get(receipt['response_ref'], f'{workflow}-patcher_agent')
        if (digest(candidate) != patch_ref.removeprefix('ctx_') or candidate != completed
                or receipt['input_version'] != version or receipt['candidate_sha256'] != candidate_hash(project, candidate)
                or sha256(raw.encode()).hexdigest() != receipt['response_sha256']
                or receipt['calls_after_patch'] < 1):
            raise ValueError('Recovery evidence mismatch')
        return receipt
    except (KeyError, TypeError, ValueError, PermissionError, OSError, AttributeError) as exc:
        raise ReconciliationRequired('补丁、输入版本或模型响应凭据核验失败，已暂停恢复。') from exc


def delivery_evidence(saved, version, source_sha, model_receipt, workers, resume_calls):
    nodes = [
        {key: event[key] for key in ('task_id', 'agent', 'attempt', 'state', 'input_refs',
                                    'input_version', 'result_sha256', 'context_ref', 'time') if key in event}
        for event in saved['events']
    ]
    return {'schema_version': 1, 'workflow_id': saved['workflow_id'], 'input_version': version,
            'nodes': nodes, 'model': model_receipt,
            'verification_workers': workers, 'verified_source_sha256': source_sha,
            'resumed': resume_calls is not None, 'model_calls_during_resume': resume_calls}
