import json
import os
import tempfile
import unittest

from cerebrum import AgentHub, AgentManifest
from aios.terminal.intent_router import IntentRouter
from runtime.launch import AIOSKernelRuntime


class TestAIOSPaperEcosystem(unittest.TestCase):
    """
    Verification suite for the 6 foundational AIOS ecosystem papers integrated into the Kernel:
    1. AIOS: LLM Agent Operating System (COLM 2025)
    2. Cerebrum (AIOS SDK): Platform for Agent Dev/Deploy/Discovery (NAACL 2025)
    3. From Commands to Prompts: LLM-based Semantic File System (LSFS) (ICLR 2025)
    4. A-Mem: Agentic Memory for LLM Agents (arXiv 2502.12110)
    5. LiteCUA: Computer as MCP Server for Computer-Use Agent on AIOS (arXiv 2505.18829)
    6. LLM as OS, Agents as Apps (arXiv 2312.03815)
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_root = os.path.join(self.temp_dir.name, "ecosystem_lsfs")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_paper4_amem_zettelkasten_linking_evolution_and_associative_recall(self) -> None:
        """A-Mem (arXiv 2502.12110): Zettelkasten note linking, neighbor evolution, and 1-hop graph retrieval."""
        with AIOSKernelRuntime(storage_root=self.storage_root, log_mode="silent") as kernel:
            mm = kernel.memory_manager

            # Note 1: foundational concept about openEuler cgroup v2
            n1 = mm.add_memory(
                "openEuler Linux kernel uses cgroup v2 for container CPU and memory isolation",
                user_id="researcher",
                keywords=["openeuler", "cgroup", "isolation"],
                tags=["os", "kernel"],
            )
            self.assertEqual(n1.evolution_count, 0)

            # Note 2: shares semantic overlap ('cgroup', 'isolation') and introduces 'pidfd' & 'oom_killer'
            n2 = mm.add_memory(
                "cgroup v2 isolation also enforces pids.max and triggers oom_killer when swap is disabled",
                user_id="researcher",
                keywords=["cgroup", "isolation", "oom_killer", "pids"],
                tags=["memory_safety"],
            )

            # Verify bidirectional Zettelkasten link and Memory Evolution on Note 1
            self.assertIn(n2.memory_id, n1.links)
            self.assertIn(n1.memory_id, n2.links)
            self.assertGreaterEqual(n1.evolution_count, 1)
            self.assertIn("memory_safety", n1.tags)
            self.assertIn("evolved#", n1.context_summary)

            # Querying only 'openeuler' recalls n1 directly AND recalls linked neighbor n2 via 1-hop expansion
            recalled = mm.search_notes(query_text="openeuler", user_id="researcher", k=2)
            recalled_ids = [r["id"] for r in recalled]
            self.assertIn(n1.memory_id, recalled_ids)
            self.assertIn(n2.memory_id, recalled_ids)

    def test_paper3_lsfs_from_commands_to_prompts_and_share_links(self) -> None:
        """From Commands to Prompts (ICLR 2025): IntentRouter prompt-to-syscall & LSFS share/rollback."""
        with AIOSKernelRuntime(storage_root=self.storage_root, log_mode="silent") as kernel:
            router = IntentRouter()

            # 1. Natural language write prompts
            w1 = router.execute_prompt(kernel, "write 'v1: initial kernel design' to specs/design.md")
            self.assertEqual(w1["mapped_syscall"], "write")
            router.execute_prompt(kernel, "write 'v2: accidental overwrite' to specs/design.md")

            # 2. Natural language rollback prompt
            rb = router.execute_prompt(kernel, "请帮我回滚 specs/design.md 1 个版本")
            self.assertEqual(rb["mapped_syscall"], "rollback")
            self.assertEqual(rb["response"]["response_message"], "v1: initial kernel design")

            # 3. Natural language share prompt -> verifiable lsfs://share/<token> link
            sh = router.execute_prompt(kernel, "share specs/design.md with reviewer_agent")
            self.assertEqual(sh["mapped_syscall"], "share")
            share_info = json.loads(sh["response"]["response_message"])
            self.assertTrue(share_info["share_link"].startswith("lsfs://share/share_"))
            self.assertEqual(share_info["shared_with"], "reviewer_agent")

            # 4. Natural language semantic retrieval prompt
            ret = router.execute_prompt(kernel, "retrieve files about kernel design")
            self.assertEqual(ret["mapped_syscall"], "retrieve")
            docs = json.loads(ret["response"]["response_message"])
            self.assertEqual(docs[0]["file_path"], "specs/design.md")

    def test_paper5_litecua_computer_as_mcp_server(self) -> None:
        """LiteCUA (arXiv 2505.18829): VMController accessibility tree observation, GUI actions & snapshot restore."""
        with AIOSKernelRuntime(storage_root=self.storage_root, log_mode="silent") as kernel:
            from cerebrum import CerebrumClient
            client = CerebrumClient(local_executor=kernel.execute_request)

            # 1. Save clean VM snapshot
            client.call_tool("cua_agent", "litecua/snapshot", {"action": "save", "name": "before_edit"})

            # 2. Observe accessibility tree
            obs = client.call_tool("cua_agent", "litecua/observe", {})["response"].response_message
            self.assertEqual(obs["active_window"], "AIOS_Code_Workspace")
            elem_ids = [el["element_id"] for el in obs["a11y_tree"]]
            self.assertIn("input_search_bar", elem_ids)

            # 3. Click search bar, type query, press hotkey
            click_res = client.call_tool(
                "cua_agent", "litecua/click", {"element_id": "input_search_bar"}
            )["response"].response_message
            self.assertEqual(click_res["element_id"], "input_search_bar")

            type_res = client.call_tool(
                "cua_agent",
                "litecua/type",
                {"text": "FIFOScheduler", "append": False},
            )["response"].response_message
            self.assertEqual(type_res["value"], "FIFOScheduler")

            hk_res = client.call_tool(
                "cua_agent", "litecua/hotkey", {"keys": ["Ctrl", "Shift", "F"]}
            )["response"].response_message
            self.assertEqual(hk_res["combo"], "ctrl+shift+f")

            # 4. Restore VM snapshot and verify state rolled back cleanly
            client.call_tool("cua_agent", "litecua/snapshot", {"action": "restore", "name": "before_edit"})
            obs_after = client.call_tool("cua_agent", "litecua/observe", {})["response"].response_message
            search_box = next(el for el in obs_after["a11y_tree"] if el["element_id"] == "input_search_bar")
            self.assertEqual(search_box["value"], "")

    def test_paper2_and_6_cerebrum_agent_hub_and_agents_as_apps_isolation(self) -> None:
        """Cerebrum (NAACL 2025) & LLM as OS, Agents as Apps (arXiv 2312.03815): AgentHub & Manifest sandboxing."""
        with AIOSKernelRuntime(storage_root=self.storage_root, log_mode="silent") as kernel:
            hub = AgentHub()

            # Publish a read-only math calculator App that only permits ["llm", "tool"] and tool "math/calculate"
            manifest = AgentManifest(
                app_id="aios_store/calc_assistant",
                name="Calculator Assistant App",
                version="1.2.0",
                description="Performs safe arithmetic and explains results",
                required_syscalls=["llm", "tool"],
                allowed_tools=["math/calculate"],
                resource_quota={
                    "cpu_milli": 500,
                    "memory_bytes": 64 * 1024 * 1024,
                    "pids": 4,
                    "max_concurrent": 1,
                    "max_model_calls": 2,
                    "max_tokens": 4096,
                },
            )

            def calc_app_entry(app_client, task_input):
                if task_input.get("try_unauthorized_storage"):
                    app_client.storage_op("calc_app", "read", {"file_path": "secret.txt"})
                if task_input.get("try_unauthorized_tool"):
                    app_client.call_tool("calc_app", "litecua/observe", {})
                calc = app_client.call_tool(
                    "calc_app",
                    "math/calculate",
                    {"expression": task_input["expression"]},
                )["response"].response_message
                chat = app_client.llm_chat(
                    "calc_app",
                    messages=[{"role": "user", "content": f"Result is {calc['result']}"}],
                )["response"].response_message
                return {"calc": calc["result"], "explanation": chat}

            hub.publish(manifest, calc_app_entry)

            # 1. Discover published App in AgentHub
            found = hub.discover("calculator")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["version"], "1.2.0")

            # 2. Run valid App invocation -> provisions ACB quota and records model_calls_used
            run_out = hub.run_app(
                "aios_store/calc_assistant",
                kernel=kernel,
                task_input={"expression": "25 * 4"},
            )
            self.assertEqual(run_out["output"]["calc"], 100)
            self.assertEqual(run_out["acb_snapshot"]["model_calls_used"], 1)

            # 3. Verify Manifest boundary blocks undeclared Storage syscalls & undeclared tools
            with self.assertRaises(PermissionError):
                hub.run_app(
                    "aios_store/calc_assistant",
                    kernel=kernel,
                    task_input={"expression": "1+1", "try_unauthorized_storage": True},
                )
            with self.assertRaises(PermissionError):
                hub.run_app(
                    "aios_store/calc_assistant",
                    kernel=kernel,
                    task_input={"expression": "1+1", "try_unauthorized_tool": True},
                )


if __name__ == "__main__":
    unittest.main()
