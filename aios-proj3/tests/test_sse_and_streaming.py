import io
import json
import unittest

from aios.llm_core.profiles import ModelProfileBroker
from aios.llm_core.sse import payloads
from aios.llm_core.streaming import (
    ModelStreamError,
    error_metadata,
    parse_chat_completions_sse_stream,
    parse_responses_sse_stream,
)
from tools.doctor import inspect_environment
from tools.validate_stream_formats import (
    build_chat_completions_sse_wire,
    build_responses_sse_wire,
    run_format_validation,
)


class TestSSEAndStreamingSuite(unittest.TestCase):
    def test_utf8_split_crlf_bom_comments_and_multiline(self) -> None:
        text = '\ufeff: heartbeat\r\nevent: message\r\ndata: {"type":\r\ndata: "fixture", "text":"公式\\n代码中文"}\r\n\r\n'

        class OneByteStream(io.BytesIO):
            def read1(self, n: int = -1) -> bytes:
                return self.read(1)

        rows = list(payloads(OneByteStream(text.encode("utf-8")), 4096))
        self.assertEqual(json.loads(rows[0]), {"type": "fixture", "text": "公式\n代码中文"})

    def test_cr_only_lf_unterminated_and_wire_limits(self) -> None:
        self.assertEqual(list(payloads(io.BytesIO(b"data: one\r\rdata: two\n\n"), 128)), ["one", "two"])
        self.assertEqual(list(payloads(io.BytesIO(b'data: {"status":"completed"}\n'), 128)), [])
        for bad in (b"data: " + b"a" * 129 + b"\n\n", b":\n\n" * 400):
            with self.assertRaises(ValueError):
                list(payloads(io.BytesIO(bad), 128))
        with self.assertRaises(UnicodeError):
            list(payloads(io.BytesIO(b"data: \xff\n\n"), 128))

    def test_chat_completions_and_responses_sse_privacy_and_completeness(self) -> None:
        events = []
        wire = build_chat_completions_sse_wire("第一段：**正文**。\n第二段完成。", multiline=True)
        res = parse_chat_completions_sse_stream(io.BytesIO(wire), observer=events.append)
        self.assertEqual(res["content"], "第一段：**正文**。\n第二段完成。")
        self.assertGreaterEqual(len(events), 2)
        self.assertNotIn("PRIVATE_CHAIN_OF_THOUGHT", str(events))

        # Incomplete stream without [DONE] must raise ModelStreamError
        truncated_wire = wire.replace(b"data: [DONE]\n\n", b"")
        with self.assertRaises(ModelStreamError):
            parse_chat_completions_sse_stream(io.BytesIO(truncated_wire))

        # Responses API stream with corrupted final text hash must be rejected
        resp_wire = build_responses_sse_wire("Expected exact output")
        tampered = resp_wire.replace(b'"text": "Expected exact output"', b'"text": "Tampered final output"')
        with self.assertRaises(ModelStreamError):
            parse_responses_sse_stream(io.BytesIO(tampered))

        # Error metadata only returns whitelist numeric codes
        meta = error_metadata({"error": {"code": "rate_limit_exceeded", "param": "max_output_tokens", "message": "sk-secret123456"}})
        self.assertEqual(meta, {"error_kind": 1, "error_param": 3})

    def test_four_stream_format_cases_across_both_protocols(self) -> None:
        report = run_format_validation()
        self.assertEqual(report["attempts"], 8)
        self.assertEqual(report["passed"], 8)

    def test_model_profile_broker_ram_isolation_and_secret_redaction(self) -> None:
        broker = ModelProfileBroker()
        secret_key = "sk-live-secret-token-1234567890abcdef"
        broker.save_profile(
            {
                "id": "aizex_main",
                "label": "Relay GPT-6",
                "provider": "aizex",
                "model": "gpt-6-luna",
                "key": secret_key,
            }
        )
        broker.select_profile("aizex_main")
        status = broker.status()
        info = broker.profiles_info()
        self.assertTrue(status["configured"])
        self.assertEqual(status["model"], "gpt-6-luna")
        self.assertNotIn(secret_key, json.dumps(status))
        self.assertNotIn(secret_key, json.dumps(info))

        # Reject credential leakage inside label or model name
        with self.assertRaises(ValueError):
            broker.save_profile(
                {
                    "id": "bad_prof",
                    "label": f"Leak {secret_key}",
                    "provider": "openai",
                    "model": "gpt-4o",
                    "key": secret_key,
                }
            )

    def test_doctor_environment_inspection(self) -> None:
        doc = inspect_environment()
        self.assertTrue(doc["python_supported"])
        self.assertTrue(doc["core_ready"])
        self.assertEqual(doc["architecture"], "AIOS-Formal-Kernel")


if __name__ == "__main__":
    unittest.main()
