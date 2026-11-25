import json
import unittest
from unittest.mock import patch

from chat_checks import extract_checks
from code_runner import find_runs, run_code
from desktop_bridge import Provider, chat

COLLATZ = '''def T(n):
    c = 0
    while n != 1:
        n = n // 2 if n % 2 == 0 else 3 * n + 1
        c += 1
    return c
n = next(n for n in range(10001, 10**6) if T(n) >= 260)
print(n, T(n))
'''


class RunnerTests(unittest.TestCase):
    def test_runs_code(self):
        r = run_code(COLLATZ)
        self.assertTrue(r["ok"])
        self.assertEqual(r["stdout"].strip(), "10971 267")

    def test_guards(self):
        for code in ("import socket; socket.create_connection(('example.com', 80))",
                     "import subprocess; subprocess.run(['echo'])",
                     "import os; os.system('echo hi')",
                     "open('/etc/hosts').read()",
                     "open('../escape.txt', 'w').write('x')",
                     "import os; os.listdir('/')",
                     "import ctypes"):
            with self.subTest(code=code):
                r = run_code(code, timeout=5)
                self.assertFalse(r["ok"])
                self.assertRegex(r["stderr"], "PermissionError|ModuleNotFoundError")

    def test_timeout_and_local_files(self):
        self.assertTrue(run_code("while True: pass", timeout=1)["timed_out"])
        r = run_code("open('a.txt','w').write('hi'); print(open('a.txt').read())", timeout=5)
        self.assertEqual(r["stdout"].strip(), "hi")

    def test_find_runs(self):
        text = "x\n```python run\nprint(1)\n```\n```python\nprint(2)\n```\n```py run\nprint(3)\n```"
        self.assertEqual([c.strip() for c in find_runs(text)], ["print(1)", "print(3)"])


class CollatzCheckTests(unittest.TestCase):
    def test_collatz_claims(self):
        got = {(c["claim"], c["verdict"]) for c in extract_checks(
            "In the Collatz sequence, 10,971 takes 267 steps, while 27 takes 110 steps.")}
        self.assertEqual(got, {("10971 takes 267 steps", "supported"), ("27 takes 110 steps", "refuted")})

    def test_code_is_not_a_claim(self):
        self.assertEqual(extract_checks("```python\nprint(2 + 2 = 5)\n```"), [])


class LoopTests(unittest.TestCase):
    def test_model_runs_code_then_answers(self):
        replies = iter(["Likely 10971. Checking.\n```python run\n" + COLLATZ + "```",
                        "Confirmed: in the Collatz sequence 10971 takes 267 steps."])
        seen, tools = [], []

        def fake_stream(self, messages, system="", on_delta=lambda s: None, cancelled=lambda: False,
                        on_thinking=lambda s: None, tools=False):
            seen.append(messages)
            self.last_thinking = ""
            self.last_tool_calls = []
            text = next(replies)
            on_delta(text)
            return text

        with patch.object(Provider, "stream", fake_stream):
            r = chat({"provider": {"name": "ollama-local", "model": "m"}, "code": True,
                      "messages": [{"role": "user", "content": "smallest n > 10000 with 260+ Collatz steps?"}]},
                     None, on_delta=lambda t: None, on_tool=tools.append)
        self.assertEqual(r["runs"], 1)
        self.assertEqual(tools[0]["stdout"].strip(), "10971 267")
        self.assertIn("10971 267", seen[1][-1]["content"])          # results fed back as next user turn
        self.assertEqual([t["role"] for t in r["turns"]], ["assistant", "user", "assistant"])
        self.assertEqual([s["type"] for s in r["segments"]], ["text", "run", "text"])
        self.assertIn(("10971 takes 267 steps", "supported"), {(c["claim"], c["verdict"]) for c in r["checks"]})

    def test_code_off_never_runs(self):
        def fake_stream(self, messages, system="", on_delta=lambda s: None, cancelled=lambda: False,
                        on_thinking=lambda s: None, tools=False):
            self.last_thinking = ""
            self.last_tool_calls = []
            return "```python run\nprint('should not run')\n```"
        with patch.object(Provider, "stream", fake_stream), patch("desktop_bridge.run_code") as rc:
            r = chat({"provider": {"name": "ollama-local", "model": "m"},
                      "messages": [{"role": "user", "content": "q"}]}, None, on_delta=lambda t: None)
        rc.assert_not_called()
        self.assertEqual(r["runs"], 0)


import io
import urllib.error
from desktop_bridge import ProviderError

CODE_JSON = json.dumps({"code": "print(6 * 7)"})


def sse(*events):
    return ["data: " + json.dumps(e) for e in events] + ["data: [DONE]"]


def native_streams():
    """First reply asks for the tool, second reply answers; per provider wire format."""
    return {
        "ollama-cloud": [
            [json.dumps({"message": {"content": "", "thinking": "compute it"}}),
             json.dumps({"message": {"content": "", "tool_calls": [{"function": {"name": "run_python",
                         "arguments": {"code": "print(6 * 7)"}}}]}}),
             json.dumps({"message": {"content": ""}, "done": True})],
            [json.dumps({"message": {"content": "The answer is 42."}, "done": True})]],
        "openrouter": [
            sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "run_python",
                 "arguments": CODE_JSON[:10]}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": CODE_JSON[10:]}}]},
                              "finish_reason": "tool_calls"}]}),
            sse({"choices": [{"delta": {"content": "The answer is 42."}}]})],
        "anthropic": [
            sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Checking."}},
                {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu1",
                 "name": "run_python", "input": {}}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta",
                 "partial_json": CODE_JSON}},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}),
            sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "The answer is 42."}})],
        "openai": [
            sse({"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "fc1",
                 "name": "run_python", "arguments": CODE_JSON}}),
            sse({"type": "response.output_text.delta", "delta": "The answer is 42."})],
    }


class NativeToolTests(unittest.TestCase):
    def test_every_provider_round_trips_a_tool_call(self):
        for name, rounds in native_streams().items():
            with self.subTest(name=name):
                it, bodies = iter(rounds), []

                def fake_lines(self, path, body, cancelled):
                    bodies.append(json.loads(json.dumps(body)))
                    return iter(next(it))

                with patch.object(Provider, "_stream_lines", fake_lines):
                    r = chat({"provider": {"name": name, "key": "k", "model": "m"}, "code": True,
                              "messages": [{"role": "user", "content": "6*7?"}]}, None, on_delta=lambda t: None)
                self.assertEqual(r["runs"], 1)
                self.assertEqual([s["stdout"].strip() for s in r["segments"] if s["type"] == "run"], ["42"])
                self.assertIn("tools", bodies[0])
                second = json.dumps(bodies[1])
                self.assertIn("42", second)                     # tool output sent back natively
                self.assertTrue(r["text"].endswith("The answer is 42."))
                self.assertEqual(r["turns"][-1]["content"], "The answer is 42.")

    def test_falls_back_to_code_blocks_when_tools_rejected(self):
        calls = []

        def fake_lines(self, path, body, cancelled):
            calls.append(body)
            if "tools" in body:
                raise ProviderError(self.name, 400, "model does not support tools")
            if len(calls) == 2:
                return iter([json.dumps({"message": {"content": "```python run\nprint(6*7)\n```"}, "done": True})])
            return iter([json.dumps({"message": {"content": "42."}, "done": True})])

        with patch.object(Provider, "_stream_lines", fake_lines):
            r = chat({"provider": {"name": "ollama-local", "model": "m"}, "code": True,
                      "messages": [{"role": "user", "content": "q"}]}, None, on_delta=lambda t: None)
        self.assertEqual(r["runs"], 1)
        self.assertEqual(r["segments"][0]["type"], "notice")
        self.assertIn("python run", calls[1]["messages"][0]["content"])   # fenced prompt after fallback

    def test_retries_server_errors_then_succeeds(self):
        attempts = []

        def flaky(req, timeout=0):
            attempts.append(1)
            if len(attempts) < 3:
                raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, io.BytesIO(b"{}"))
            return io.BytesIO((json.dumps({"message": {"content": "ok"}, "done": True}) + "\n").encode())

        with patch("desktop_bridge.urllib.request.urlopen", flaky), patch("desktop_bridge.time.sleep"):
            text = Provider("ollama-local", model="m").stream([{"role": "user", "content": "q"}])
        self.assertEqual((text, len(attempts)), ("ok", 3))


if __name__ == "__main__":
    unittest.main()
