import sqlite3
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aios.context.context_store import ContextStore
from cerebrum.llm.apis import LLMQuery, LLMResponse
from runtime.launch import AIOSKernelRuntime


class _FailNextCommitConnection:
    """Real SQLite connection proxy that injects a single OperationalError on commit()."""

    def __init__(self, real_conn: sqlite3.Connection):
        self._real = real_conn
        self.fail_next_commit = False

    def execute(self, *args, **kwargs):
        return self._real.execute(*args, **kwargs)

    def commit(self):
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise sqlite3.OperationalError("injected commit failure")
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        return self._real.close()


class PerfSandboxInvariantTests(unittest.TestCase):
    def test_failed_duplicate_put_cannot_commit_authorization_later(self):
        with TemporaryDirectory() as tmp:
            store = ContextStore(tmp, persistent_connection=True)
            try:
                ctx_id = store.put("owner1", {"secret": "v1"})
                proxy = _FailNextCommitConnection(store._db)
                store._db = proxy
                proxy.fail_next_commit = True

                with self.assertRaises(sqlite3.OperationalError):
                    store.put("owner1", {"secret": "v1"}, authorized_agents=["intruder"])

                # Unrelated write succeeds; must NOT commit the rolled-back authorization
                store.put("owner1", {"other": "v2"})
            finally:
                store.close()

            reopened = ContextStore(tmp, persistent_connection=True)
            try:
                with self.assertRaises(PermissionError):
                    reopened.get(ctx_id, reader_agent="intruder")
            finally:
                reopened.close()

    def test_failed_batch_grant_cannot_commit_on_unrelated_write(self):
        with TemporaryDirectory() as tmp:
            store = ContextStore(tmp, persistent_connection=True)
            try:
                c1 = store.put("owner1", {"doc": 1})
                c2 = store.put("owner1", {"doc": 2})
                proxy = _FailNextCommitConnection(store._db)
                store._db = proxy
                proxy.fail_next_commit = True

                with self.assertRaises(sqlite3.OperationalError):
                    store.grant_access_batch([c1, c2], granter_agent="owner1", grantee_agent="intruder")

                # Unrelated write succeeds
                store.put("owner1", {"doc": 3})
            finally:
                store.close()

            reopened = ContextStore(tmp, persistent_connection=True)
            try:
                with self.assertRaises(PermissionError):
                    reopened.get(c1, reader_agent="intruder")
                with self.assertRaises(PermissionError):
                    reopened.get(c2, reader_agent="intruder")
            finally:
                reopened.close()

    def test_rr_scheduler_restart_does_not_consume_stale_sentinel(self):
        with TemporaryDirectory() as tmp:
            kernel = AIOSKernelRuntime(
                scheduler_policy="rr",
                storage_root=str(Path(tmp) / "store"),
                log_mode="silent",
            ).start()
            try:
                started = threading.Event()
                release = threading.Event()
                orig_exec = kernel.llm.execute_llm_syscall

                def slow_once(syscall):
                    if not started.is_set():
                        started.set()
                        release.wait(timeout=2.0)
                    return orig_exec(syscall)

                kernel.llm.execute_llm_syscall = slow_once

                def run_first():
                    kernel.execute_request(
                        "agent_rr",
                        LLMQuery(model="mock-aios-core", messages=[{"role": "user", "content": "first"}], max_new_tokens=32),
                    )

                t = threading.Thread(target=run_first, daemon=True)
                t.start()
                self.assertTrue(started.wait(timeout=2.0))

                # Stop scheduler while LLM consumer is busy executing the first syscall
                stop_thread = threading.Thread(target=kernel.scheduler.stop, daemon=True)
                stop_thread.start()
                time.sleep(0.05)
                release.set()
                stop_thread.join(timeout=3.0)
                t.join(timeout=3.0)
                self.assertFalse(kernel.scheduler.active)

                # Restart scheduler and verify a fresh request completes without hanging
                kernel.scheduler.start()
                done = threading.Event()
                result_box = {}

                def run_second():
                    result_box["out"] = kernel.execute_request(
                        "agent_rr",
                        LLMQuery(model="mock-aios-core", messages=[{"role": "user", "content": "second"}], max_new_tokens=32),
                    )
                    done.set()

                t2 = threading.Thread(target=run_second, daemon=True)
                t2.start()
                self.assertTrue(done.wait(timeout=3.0), "Restarted RR scheduler hung on stale wake sentinel")
                self.assertEqual(result_box["out"]["response"].status_code, 200)
            finally:
                kernel.stop()


if __name__ == "__main__":
    unittest.main()
