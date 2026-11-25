import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from chat_checks import evaluate, extract_checks, is_prime
from desktop_bridge import Provider, chat, serve


def sse(*events):
    return ["data: " + json.dumps(e) for e in events] + ["data: [DONE]"]


STREAMS = {
    "openai": sse({"type": "response.output_text.delta", "delta": "2 + 2 "},
                  {"type": "response.output_text.delta", "delta": "= 5"},
                  {"type": "response.completed", "response": {"usage": {"output_tokens": 3}}}),
    "anthropic": sse({"type": "message_start", "message": {"usage": {"input_tokens": 9}}},
                     {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "2 + 2 "}},
                     {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "= 5"}},
                     {"type": "message_delta", "usage": {"output_tokens": 3}}),
    "openrouter": sse({"choices": [{"delta": {"content": "2 + 2 "}}]}, {"choices": [{"delta": {"content": "= 5"}}]}),
    "ollama-local": [json.dumps({"message": {"content": "2 + 2 "}}),
                     json.dumps({"message": {"content": "= 5"}, "done": True, "eval_count": 3})],
}


class ChecksTests(unittest.TestCase):
    def test_arithmetic(self):
        ok = extract_checks("We get `40² + 40 + 41 = 1681 = 41²`.")
        self.assertEqual([c["verdict"] for c in ok], ["supported"])
        bad = extract_checks("Clearly 7 * 8 = 54.")
        self.assertEqual(bad[0]["verdict"], "refuted")
        self.assertEqual(extract_checks("Set x = 5 and n == 3."), [])

    def test_evaluate_is_bounded(self):
        for expr in ("9**999999", "__import__('os')", "a + 1", ""):
            with self.assertRaises((ValueError, SyntaxError)):
                evaluate(expr)

    def test_primality_and_letters(self):
        kinds = {(c["kind"], c["verdict"]) for c in extract_checks(
            "91 is prime. 97 is prime. 1681 is not prime. There are 3 r's in strawberry; two r's in strawberry.")}
        self.assertEqual(kinds, {("primality", "refuted"), ("primality", "supported"),
                                 ("letters", "supported"), ("letters", "refuted")})
        self.assertTrue(is_prime(2) and not is_prime(1) and not is_prime(91))


class StreamTests(unittest.TestCase):
    def test_stream_every_provider(self):
        for name, lines in STREAMS.items():
            with self.subTest(name=name), patch.object(Provider, "_stream_lines", return_value=iter(lines)):
                got = []
                text = Provider(name, key="k", model="m").stream([{"role": "user", "content": "q"}], "", got.append)
                self.assertEqual(text, "2 + 2 = 5")
                self.assertEqual(got, ["2 + 2 ", "= 5"])

    def test_chat_checks_feed_persistent_state(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.object(Provider, "_stream_lines", side_effect=lambda *a: iter(STREAMS["ollama-local"])):
            req = {"provider": {"name": "ollama-local", "model": "m"}, "messages": [{"role": "user", "content": "q"}]}
            first = chat(req, Path(d), on_delta=lambda t: None)
            self.assertEqual(first["checks"][0]["verdict"], "refuted")
            self.assertGreater(first["state"]["surprise"], 0)
            second = chat(req, Path(d), on_delta=lambda t: None)
            self.assertGreater(second["state"]["surprise"], first["state"]["surprise"])


class ServeTests(unittest.TestCase):
    def test_concurrent_requests_and_stream(self):
        r, w = io.StringIO(), io.StringIO()
        reqs = [{"id": 1, "action": "ping"},
                {"id": 2, "action": "chat", "stream": True, "provider": {"name": "ollama-local", "model": "m"},
                 "messages": [{"role": "user", "content": "q"}]},
                {"id": 3, "action": "nope"}]
        r.write("".join(json.dumps(x) + "\n" for x in reqs)); r.seek(0)
        with tempfile.TemporaryDirectory() as d, \
                patch.object(Provider, "_stream_lines", side_effect=lambda *a: iter(STREAMS["ollama-local"])):
            serve(Path(d), r, w)
            deadline = time.time() + 5
            while time.time() < deadline and sum(1 for l in w.getvalue().splitlines() if '"ok"' in l) < 3:
                time.sleep(.02)
        out = [json.loads(l) for l in w.getvalue().splitlines()]
        self.assertTrue(out[0]["ready"])
        done = {o["id"]: o for o in out if "ok" in o}
        self.assertEqual(done[1]["value"], "pong")
        self.assertEqual(done[2]["value"]["text"], "2 + 2 = 5")
        self.assertFalse(done[3]["ok"])
        self.assertEqual([o["delta"] for o in out if o.get("id") == 2 and "delta" in o], ["2 + 2 ", "= 5"])


if __name__ == "__main__":
    unittest.main()
