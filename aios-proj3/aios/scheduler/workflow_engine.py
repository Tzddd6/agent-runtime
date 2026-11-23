import json
import hashlib
from types import CodeType
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import os
import re
import time
from collections import OrderedDict
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from cerebrum.llm.apis import LLMQuery
from cerebrum.memory.apis import MemoryQuery
from cerebrum.storage.apis import StorageQuery
from cerebrum.tool.apis import ToolQuery
from aios.scheduler.drf_admission import ResourceVector


def validate_contract(output: Any, contract: Optional[Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
    """
    Declarative Result Contract Validator (Proj3 Requirement 4.4).
    Prevents empty/malformed Agent outputs from polluting downstream branches.
    """
    if not contract:
        return True, None
    if contract.get("non_empty"):
        if output is None or output == "" or output == {} or output == []:
            return False, "Contract violation: result must be non-empty"
    if contract.get("type") == "dict" or contract.get("required_keys"):
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except Exception:
                return False, "Contract violation: output is not valid JSON object"
        if not isinstance(output, dict):
            return False, "Contract violation: output must be a dictionary/JSON object"
        for req_key in contract.get("required_keys", []):
            if req_key not in output or output[req_key] in (None, ""):
                return False, f"Contract violation: missing or empty required key '{req_key}'"
    return True, None


class WorkflowEngine:
    """
    AIOS Kernel Multi-Agent DAG & Dynamic Task Orchestrator (Proj3 Requirements 4.2, 4.3, 4.4, 4.5, 4.7).
    Executes multi-agent task graphs over the AIOS Kernel Syscall layer with:
    - Task dependency DAG & cycle detection
    - Dynamic task spawning (`spawn_tasks`) during execution with explicit context ACL delegation
    - Resource-aware DRF admission (`cpu_milli`, `memory_bytes`, `pids`)
    - Single-agent fault isolation (bounded retries, declarative contracts, failed branch isolation
      without cascading failure to healthy parallel branches)
    - Persistent checkpoint saving & safe recovery
    """

    def __init__(self, kernel: Any, checkpoint_dir: Optional[str] = None):
        self.kernel = kernel
        self.checkpoint_dir = checkpoint_dir
        self._graphs = OrderedDict()
        self._active_workflows = set()
        self._plan_signatures = {}
        self._graphs_lock = Lock()
        if self.checkpoint_dir:
            os.makedirs(self.checkpoint_dir, exist_ok=True)

    def recent_graphs(self) -> List[Dict[str, Any]]:
        with self._graphs_lock:
            return json.loads(json.dumps(list(reversed(self._graphs.values()))))

    def _publish_graph(self, workflow_id, tasks, states, attempts):
        nodes = [{"id": key, "agent": task.get("agent", key), "state": states[key],
                  "depends_on": list(task.get("depends_on", [])), "attempts": attempts[key]}
                 for key, task in tasks.items()]
        with self._graphs_lock:
            self._graphs[workflow_id] = {"id": workflow_id, "nodes": nodes[:100], "truncated": len(nodes) > 100}
            self._graphs.move_to_end(workflow_id)
            if len(self._graphs) > 20:
                self._graphs.popitem(last=False)
        return nodes

    @staticmethod
    def _detect_cycle(tasks_by_id: Dict[str, Dict[str, Any]]) -> Optional[List[str]]:
        visiting: Set[str] = set()
        visited: Set[str] = set()

        def dfs(node_id: str, path: List[str]) -> Optional[List[str]]:
            if node_id in visiting:
                return path + [node_id]
            if node_id in visited:
                return None
            visiting.add(node_id)
            task = tasks_by_id.get(node_id, {})
            for dep in task.get("depends_on", []):
                if dep not in tasks_by_id:
                    raise ValueError(f"Task '{node_id}' depends on non-existent task '{dep}'")
                cycle = dfs(dep, path + [node_id])
                if cycle:
                    return cycle
            visiting.remove(node_id)
            visited.add(node_id)
            return None

        for tid in tasks_by_id:
            cycle = dfs(tid, [])
            if cycle:
                return cycle
        return None

    def _build_query_from_task(self, task: Dict[str, Any], upstream_payloads: Dict[str, Any]):
        query = task.get("query")
        if query is not None:
            return query

        q_type = task.get("query_type", "llm").lower()
        q_data = dict(task.get("query_data") or {})

        if q_type == "llm":
            messages = list(q_data.get("messages") or [])
            if upstream_payloads:
                messages.append(
                    {
                        "role": "system",
                        "content": f"[Upstream Context]: {json.dumps(upstream_payloads, ensure_ascii=False)}",
                    }
                )
            q_data["messages"] = messages
            return LLMQuery(**q_data)
        if q_type == "tool":
            return ToolQuery(**q_data)
        if q_type == "memory":
            return MemoryQuery(**q_data)
        if q_type == "storage":
            return StorageQuery(**q_data)
        raise ValueError(f"Unsupported task query_type: {q_type}")

    def _save_checkpoint(
        self,
        workflow_id: str,
        states: Dict[str, str],
        results: Dict[str, Any],
        context_refs: Dict[str, str],
        events: List[Dict[str, Any]],
    ) -> Optional[str]:
        if not self.checkpoint_dir:
            return None
        cp_path = os.path.join(self.checkpoint_dir, f"{workflow_id}_checkpoint.json")
        tmp_path = cp_path + ".tmp"
        payload = {
            "workflow_id": workflow_id,
            "plan_signature": self._plan_signatures.get(workflow_id),
            "updated_at": time.time(),
            "states": states,
            "results": results,
            "context_refs": context_refs,
            "events": events,
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_path, cp_path)
        return cp_path

    def load_checkpoint(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        self._validate_workflow_id(workflow_id)
        if not self.checkpoint_dir:
            return None
        cp_path = os.path.join(self.checkpoint_dir, f"{workflow_id}_checkpoint.json")
        if not os.path.exists(cp_path):
            return None
        with open(cp_path, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _validate_workflow_id(workflow_id: str) -> None:
        if not isinstance(workflow_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}", workflow_id):
            raise ValueError("Invalid workflow identifier")

    @staticmethod
    def _plan_signature(tasks):
        def code_signature(code):
            constants = [code_signature(item) if isinstance(item, CodeType) else repr(item) for item in code.co_consts]
            return [code.co_code.hex(), code.co_names, code.co_varnames, constants]
        def normalize(value):
            if callable(value):
                code = getattr(value, '__code__', None)
                if code is None:
                    raise ValueError('Recovery requires inspectable handlers')
                captured = [cell.cell_contents for cell in (getattr(value, '__closure__', None) or ())
                            if isinstance(cell.cell_contents, (str, int, float, bool, type(None)))]
                return {'code': code_signature(code), 'captured': captured}
            if hasattr(value, 'to_dict'):
                return normalize(value.to_dict())
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [normalize(item) for item in value]
            return value
        raw = json.dumps(normalize(tasks), ensure_ascii=False, sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()

    def run_workflow(
        self,
        workflow_id: str,
        tasks: List[Dict[str, Any]],
        resume: bool = False,
        byte_budget_per_dep: int = 512,
        max_workers: int = 4,
        admission_timeout: float = 30.0,
    ) -> Dict[str, Any]:
        self._validate_workflow_id(workflow_id)
        plan_signature = self._plan_signature(tasks)
        if type(max_workers) is not int or not 1 <= max_workers <= 32:
            raise ValueError("max_workers must be between 1 and 32")
        if not 0 < admission_timeout <= 300:
            raise ValueError("admission_timeout must be between 0 and 300")
        with self._graphs_lock:
            if workflow_id in self._active_workflows:
                raise ValueError("Workflow is already running")
            self._active_workflows.add(workflow_id)
            self._plan_signatures[workflow_id] = plan_signature
        try:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="aios-dag") as pool:
                return self._run_workflow(workflow_id, tasks, resume, byte_budget_per_dep,
                                          pool, max_workers, admission_timeout)
        except Exception:
            with self._graphs_lock:
                for node in self._graphs.get(workflow_id, {}).get("nodes", []):
                    if node["state"] in ("waiting", "running"):
                        node["state"] = "interrupted"
            raise
        finally:
            with self._graphs_lock:
                self._active_workflows.remove(workflow_id)
                self._plan_signatures.pop(workflow_id, None)

    def _execute_task(self, task, upstream_views, attempt_no, held_vec):
        drf = self.kernel.scheduler.drf_controller
        agent_name = task.get("agent", task["id"])
        task_error: Optional[str] = None
        output_value: Any = None
        res_cfg = task.get("resources") or {}
        task_req_vec = ResourceVector(
            cpu_milli=int(res_cfg.get("cpu_milli", 250)),
            memory_bytes=int(res_cfg.get("memory_bytes", 64 * 1024 * 1024)),
            pids=int(res_cfg.get("pids", 4)),
        )
        try:
            custom_fn: Optional[Callable] = task.get("handler")
            if custom_fn is not None:
                output_value = custom_fn(task, upstream_views, attempt_no)
            else:
                query_obj = self._build_query_from_task(task, upstream_views)
                exec_out = self.kernel.execute_request(agent_name, query_obj)
                resp = exec_out.get("response")
                if resp is None or getattr(resp, "status_code", 200) >= 400 or getattr(resp, "error", None):
                    raise RuntimeError(getattr(resp, "error", None) or "Syscall execution did not succeed")
                output_value = getattr(resp, "response_message", resp)

            ok, contract_err = validate_contract(output_value, task.get("contract"))
            if not ok:
                raise ValueError(contract_err or "Contract validation failed")
        except Exception as exc:
            task_error = str(exc)

        finally:
            if held_vec is not None:
                drf.release(agent_name, held_vec)
        return output_value, task_error

    def _run_workflow(self, workflow_id, tasks, resume, byte_budget_per_dep, pool, max_workers, admission_timeout):
        tasks_by_id: Dict[str, Dict[str, Any]] = {}
        for t in tasks:
            tid = str(t["id"])
            if tid in tasks_by_id:
                raise ValueError(f"Duplicate task id: '{tid}'")
            tasks_by_id[tid] = dict(t)

        cycle = self._detect_cycle(tasks_by_id)
        if cycle:
            raise ValueError(f"Cyclic dependency detected in workflow '{workflow_id}': {' -> '.join(cycle)}")

        states: Dict[str, str] = {tid: "waiting" for tid in tasks_by_id}
        attempts: Dict[str, int] = {tid: 0 for tid in tasks_by_id}
        results: Dict[str, Any] = {}
        context_refs: Dict[str, str] = {}
        events: List[Dict[str, Any]] = []

        if resume:
            saved = self.load_checkpoint(workflow_id)
            if saved:
                if any(state == 'running' for state in saved.get('states', {}).values()):
                    raise RuntimeError('Recovery blocked: in-flight operations require reconciliation')
                if saved.get('plan_signature') != self._plan_signatures[workflow_id]:
                    raise RuntimeError('Recovery blocked: task plan changed or lacks a verifiable signature')
                saved_ids = set(saved.get('states', {}))
                known = list(tasks_by_id.values())
                for parent in known:
                    for child in parent.get('spawn_tasks', []):
                        child_id = str(child['id'])
                        if child_id in saved_ids and child_id not in tasks_by_id:
                            recovered = dict(child)
                            recovered['depends_on'] = list(dict.fromkeys([*child.get('depends_on', []), parent['id']]))
                            tasks_by_id[child_id] = recovered
                            known.append(recovered)
                            states[child_id], attempts[child_id] = 'waiting', 0
                            for join in recovered.get('wire_into', []):
                                if join in tasks_by_id:
                                    tasks_by_id[join]['depends_on'] = list(dict.fromkeys([*tasks_by_id[join].get('depends_on', []), child_id]))
                if saved_ids - tasks_by_id.keys():
                    raise RuntimeError('Recovery blocked: dynamic task definitions are unavailable')
                for tid, st in saved.get("states", {}).items():
                    if tid in states and st == "succeeded":
                        states[tid] = "succeeded"
                        results[tid] = saved.get("results", {}).get(tid)
                        if tid in saved.get("context_refs", {}):
                            context_refs[tid] = saved["context_refs"][tid]
                events.extend(saved.get("events", []))

        drf = self.kernel.scheduler.drf_controller
        ctx_store = self.kernel.context_store
        self._publish_graph(workflow_id, tasks_by_id, states, attempts)

        running = {}
        blocked_since = None
        while True:
            # 1. Propagate upstream failures/skips to dependent tasks (Fault Isolation)
            progressed_skip = False
            for tid, task in list(tasks_by_id.items()):
                if states[tid] != "waiting":
                    continue
                deps = task.get("depends_on", [])
                if any(states.get(d) in ("failed", "isolated_skipped", "unschedulable") for d in deps):
                    states[tid] = "isolated_skipped"
                    events.append(
                        {
                            "workflow_id": workflow_id,
                            "task_id": tid,
                            "agent": task.get("agent", tid),
                            "state": "isolated_skipped",
                            "reason": "upstream_dependency_failed",
                            "time": time.time(),
                        }
                    )
                    progressed_skip = True

            if progressed_skip:
                continue

            # 2. Collect ready tasks whose dependencies all succeeded
            ready_ids = [
                tid
                for tid, task in tasks_by_id.items()
                if states[tid] == "waiting"
                and all(states.get(d) == "succeeded" for d in task.get("depends_on", []))
            ]

            if not ready_ids and not running:
                break

            completed = [future for future in running if future.done()]
            if running and (completed or len(running) >= max_workers or not ready_ids):
                if not completed:
                    completed, _ = wait(running, timeout=0.05, return_when=FIRST_COMPLETED)
                    if not completed:
                        continue
                future = next(iter(completed))
                chosen_id = running.pop(future)
                task = tasks_by_id[chosen_id]
                agent_name = task.get("agent", chosen_id)
                attempt_no = attempts[chosen_id]
                max_retries = int(task.get("max_retries", 0))
                output_value, task_error = future.result()
            else:
                # 3. Filter unschedulable tasks exceeding total kernel capacity, then pick via DRF share
                admissible_ids: List[str] = []
                for tid in ready_ids:
                    task = tasks_by_id[tid]
                    res_cfg = task.get("resources") or {}
                    req_vec = ResourceVector(
                        cpu_milli=int(res_cfg.get("cpu_milli", 250)),
                        memory_bytes=int(res_cfg.get("memory_bytes", 64 * 1024 * 1024)),
                        pids=int(res_cfg.get("pids", 4)),
                    )
                    agent_name = task.get("agent", tid)
                    acb = drf.get_or_create_agent(agent_name)
                    if acb.state == "terminated":
                        states[tid] = "failed"
                        events.append(
                            {
                                "workflow_id": workflow_id,
                                "task_id": tid,
                                "agent": agent_name,
                                "state": "failed",
                                "reason": "agent_terminated",
                                "time": time.time(),
                            }
                        )
                        continue
                    if (
                        req_vec.cpu_milli > drf.capacity.cpu_milli
                        or req_vec.memory_bytes > drf.capacity.memory_bytes
                        or req_vec.pids > drf.capacity.pids
                    ):
                        states[tid] = "unschedulable"
                        events.append(
                            {
                                "workflow_id": workflow_id,
                                "task_id": tid,
                                "agent": agent_name,
                                "state": "unschedulable",
                                "reason": "exceeds_kernel_capacity",
                                "time": time.time(),
                            }
                        )
                        continue
                    if drf.can_admit(req_vec, agent_name=agent_name):
                        admissible_ids.append(tid)

                if not admissible_ids and any(states[tid] in ("failed", "unschedulable") for tid in ready_ids):
                    continue
                if not admissible_ids:
                    if not self.kernel.scheduler.active:
                        raise RuntimeError("Kernel stopped while workflow waited for admission")
                    blocked_since = blocked_since or time.monotonic()
                    if time.monotonic() - blocked_since >= admission_timeout:
                        raise TimeoutError("Workflow resource admission timed out")
                    time.sleep(0.01)
                    continue
                blocked_since = None

                # Select next task by lowest Agent DRF dominant share
                admissible_ids.sort(
                    key=lambda tid: drf.get_or_create_agent(tasks_by_id[tid].get("agent", tid)).dominant_share
                )
                chosen_id = admissible_ids[0]
                task = tasks_by_id[chosen_id]
                agent_name = task.get("agent", chosen_id)
                attempts[chosen_id] += 1
                attempt_no = attempts[chosen_id]
                max_retries = int(task.get("max_retries", 0))

                # 4. Resolve upstream context references with ACL check & budget compression
                upstream_views: Dict[str, Any] = {}
                acl_error: Optional[str] = None
                for dep_id in task.get("depends_on", []):
                    ctx_id = context_refs.get(dep_id)
                    if ctx_id and ctx_store is not None:
                        try:
                            view = ctx_store.build_budget_view(
                                ctx_id,
                                reader_agent=agent_name,
                                byte_budget=int(task.get("context_byte_budget", byte_budget_per_dep)),
                            )
                            upstream_views[dep_id] = view["content"]
                        except PermissionError as exc:
                            acl_error = str(exc)
                            break

                if acl_error is not None:
                    states[chosen_id] = "failed"
                    results[chosen_id] = {"error": acl_error}
                    events.append(
                        {
                            "workflow_id": workflow_id,
                            "task_id": chosen_id,
                            "agent": agent_name,
                            "attempt": attempt_no,
                            "state": "failed",
                            "reason": acl_error,
                            "time": time.time(),
                        }
                    )
                    continue

                # 5. Execute task via custom handler or AIOS Kernel Syscall
                states[chosen_id] = "running"
                self._publish_graph(workflow_id, tasks_by_id, states, attempts)
                events.append(
                    {
                        "workflow_id": workflow_id,
                        "task_id": chosen_id,
                        "agent": agent_name,
                        "attempt": attempt_no,
                        "state": "running",
                        "cgroup_mode": "admission_only",
                        "time": time.time(),
                    }
                )

                held_vec = None
                if task.get("handler") is not None:
                    resources = task.get("resources") or {}
                    try:
                        held_vec = drf.acquire(agent_name, ResourceVector(
                            cpu_milli=int(resources.get("cpu_milli", 250)),
                            memory_bytes=int(resources.get("memory_bytes", 64 * 1024 * 1024)),
                            pids=int(resources.get("pids", 4))))
                    except RuntimeError:
                        states[chosen_id] = "waiting"
                        attempts[chosen_id] -= 1
                        continue
                try:
                    self._save_checkpoint(workflow_id, states, results, context_refs, events)
                    future = pool.submit(self._execute_task, task, upstream_views, attempt_no, held_vec)
                except Exception:
                    if held_vec is not None:
                        drf.release(agent_name, held_vec)
                    raise
                running[future] = chosen_id
                continue

            if task_error is not None:
                if attempt_no <= max_retries:
                    states[chosen_id] = "waiting"
                    events.append(
                        {
                            "workflow_id": workflow_id,
                            "task_id": chosen_id,
                            "agent": agent_name,
                            "attempt": attempt_no,
                            "state": "retrying",
                            "error": task_error,
                            "time": time.time(),
                        }
                    )
                else:
                    states[chosen_id] = "failed"
                    results[chosen_id] = {"error": task_error}
                    events.append(
                        {
                            "workflow_id": workflow_id,
                            "task_id": chosen_id,
                            "agent": agent_name,
                            "attempt": attempt_no,
                            "state": "failed",
                            "error": task_error,
                            "time": time.time(),
                        }
                    )
                self._save_checkpoint(workflow_id, states, results, context_refs, events)
                continue

            # 6. Task succeeded: store immutable context in ContextStore & grant ACL to declared downstream consumers
            states[chosen_id] = "succeeded"
            results[chosen_id] = output_value

            downstream_agents = [
                tasks_by_id[other_id].get("agent", other_id)
                for other_id, other_t in tasks_by_id.items()
                if chosen_id in other_t.get("depends_on", [])
            ]
            if isinstance(output_value, dict) and "spawn_tasks" in output_value:
                for sp in output_value["spawn_tasks"]:
                    downstream_agents.append(sp.get("agent", sp["id"]))

            if ctx_store is not None:
                authorized = list(set(downstream_agents + list(task.get("authorize_agents", []))))
                ctx_id = ctx_store.put(
                    owner_agent=agent_name,
                    data=output_value,
                    authorized_agents=authorized,
                )
                context_refs[chosen_id] = ctx_id

            # 7. Handle Dynamic Task Spawning (`spawn_tasks`) (Proj3 Requirement 4.2)
            spawn_list = []
            if isinstance(output_value, dict) and isinstance(output_value.get("spawn_tasks"), list):
                spawn_list = output_value["spawn_tasks"]
            elif isinstance(task.get("spawn_tasks"), list):
                spawn_list = task["spawn_tasks"]

            for new_task in spawn_list:
                nt = dict(new_task)
                nt_id = str(nt["id"])
                if nt_id in tasks_by_id:
                    raise ValueError(f"Dynamic spawn duplicate task id: '{nt_id}'")
                deps = list(nt.get("depends_on") or [])
                if chosen_id not in deps:
                    deps.append(chosen_id)
                nt["depends_on"] = deps
                tasks_by_id[nt_id] = nt
                states[nt_id] = "waiting"
                attempts[nt_id] = 0

                # Delegate upstream context permissions if parent task requests `delegate_contexts`
                if ctx_store is not None:
                    target_ag = nt.get("agent", nt_id)
                    if chosen_id in context_refs:
                        ctx_store.grant_access(context_refs[chosen_id], agent_name, target_ag)
                    for inherited_dep in deps:
                        if inherited_dep in context_refs:
                            try:
                                ctx_store.grant_access(context_refs[inherited_dep], agent_name, target_ag)
                            except PermissionError:
                                pass

                # Wire newly spawned task into any existing join/barrier tasks declared in `wire_into`
                for join_tid in nt.get("wire_into", []):
                    if join_tid in tasks_by_id and states.get(join_tid) == "waiting":
                        join_deps = list(tasks_by_id[join_tid].get("depends_on") or [])
                        if nt_id not in join_deps:
                            join_deps.append(nt_id)
                            tasks_by_id[join_tid]["depends_on"] = join_deps

                cycle_after = self._detect_cycle(tasks_by_id)
                if cycle_after:
                    tasks_by_id.pop(nt_id, None)
                    states.pop(nt_id, None)
                    raise ValueError(f"Dynamic task '{nt_id}' introduced a cycle")

            events.append(
                {
                    "workflow_id": workflow_id,
                    "task_id": chosen_id,
                    "agent": agent_name,
                    "attempt": attempt_no,
                    "state": "succeeded",
                    "context_ref": context_refs.get(chosen_id),
                    "spawned": [sp["id"] for sp in spawn_list],
                    "time": time.time(),
                }
            )
            self._save_checkpoint(workflow_id, states, results, context_refs, events)
            self._publish_graph(workflow_id, tasks_by_id, states, attempts)

        graph = self._publish_graph(workflow_id, tasks_by_id, states, attempts)
        return {
            "workflow_id": workflow_id,
            "states": states,
            "graph": graph,
            "attempts": attempts,
            "results": results,
            "context_refs": context_refs,
            "context_stats": ctx_store.stats() if ctx_store is not None else {},
            "events": events,
        }
