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
                        on_thinking=lambda s: None):
            seen.append(messages)
            self.last_thinking = ""
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
                        on_thinking=lambda s: None):
            self.last_thinking = ""
            return "```python run\nprint('should not run')\n```"
        with patch.object(Provider, "stream", fake_stream), patch("desktop_bridge.run_code") as rc:
            r = chat({"provider": {"name": "ollama-local", "model": "m"},
                      "messages": [{"role": "user", "content": "q"}]}, None, on_delta=lambda t: None)
        rc.assert_not_called()
        self.assertEqual(r["runs"], 0)


if __name__ == "__main__":
    unittest.main()
