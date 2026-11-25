import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from desktop_bridge import Provider, letter_verifier, run
from runtime_v2 import Verdict


class BridgeTests(unittest.TestCase):
    def test_catalogs(self):
        for name in ("openai", "anthropic", "openrouter", "ollama-local", "ollama-cloud"):
            p = Provider(name)
            data = {"models": [{"model": "example"}]} if name.startswith("ollama") else {"data": [{"id": "example"}]}
            with patch.object(p, "request", return_value=data):
                self.assertEqual(p.models(), ["example"])

    def test_generation_shapes(self):
        fixtures = {
            "openai": {"output": [{"type": "message", "content": [{"type": "output_text", "text": "answer"}]}]},
            "anthropic": {"content": [{"type": "text", "text": "answer"}]},
            "openrouter": {"choices": [{"message": {"content": "answer"}}]},
            "ollama-cloud": {"message": {"content": "answer"}},
            "ollama-local": {"message": {"content": "answer"}},
        }
        for name, data in fixtures.items():
            with self.subTest(name=name):
                p = Provider(name, model="test")
                with patch.object(p, "request", return_value=data) as request:
                    self.assertEqual(p.generate("instruction", {}), "answer")
                    self.assertEqual(p.calls, 1)
                    self.assertEqual(request.call_args.args[1]["model"], "test")

    def test_exact_positions(self):
        v = letter_verifier("strawberry", "r")
        self.assertIs(v("task", "[3, 8, 9]", None, ())[0].verdict, Verdict.SUPPORTED)
        for claim in ("[3,8]", "[3,8,9.0]", "3", "not JSON"):
            self.assertIs(v("task", claim, None, ())[0].verdict, Verdict.REFUTED)

    def test_run(self):
        def propose(self, ctx):
            phase = ctx["phase"]
            return {"phase": phase, **{"decompose": {"subproblems": ["identify", "count"]},
                                       "hypothesize": {"claim": "[3,8,9]"},
                                       "attack": {"attacks": ["index check", "case check"]}}[phase]}
        with tempfile.TemporaryDirectory() as d, patch.object(Provider, "propose", propose):
            req = {"provider": {"name": "openai", "model": "test"}, "mode": "letters",
                   "word": "strawberry", "letter": "r", "budget": 2, "fresh": True}
            self.assertIs(run(req, Path(d))["result"]["verdict"], Verdict.SUPPORTED)
            req.update(mode="research", task="A conjecture")
            self.assertIs(run(req, Path(d))["result"]["verdict"], Verdict.INCONCLUSIVE)


if __name__ == "__main__":
    unittest.main()
