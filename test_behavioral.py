import unittest
import tempfile
from pathlib import Path

from behavioral import SearchRuntime, Dynamics, Strategy
from benchmark_behavioral import compare, proposer, verifier
from runtime_v2 import Verdict, Evidence
from trace_replay import Observation, replay


class BehavioralTests(unittest.TestCase):
    def setUp(self):
        self.runtime = SearchRuntime()
        self.addCleanup(self.runtime.close)

    def search(self, p=proposer, v=verifier, **kwargs):
        return self.runtime.search("Find a divisor", p, v,
                                   required_checks=frozenset({"remainder"}), **kwargs)

    def test_refutation_is_not_task_refutation(self):
        result = self.search(budget=1)
        self.assertEqual(result.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(result.candidates[0].verdict, Verdict.REFUTED)

    def test_strategy_changes(self):
        result = self.search(budget=4)
        self.assertEqual(result.verdict, Verdict.SUPPORTED)
        self.assertEqual(result.supported_claims, ("1681 is divisible by 41",))
        self.assertEqual(result.candidates[1].strategy, Strategy.COUNTEREXAMPLE)

    def test_budget_and_duplicates(self):
        result = self.search(budget=3, adaptive=False)
        events = self.runtime.history(result.run_id)
        self.assertEqual(sum(e["kind"] == "strategy" for e in events), 3)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(sum(e["data"].get("event") == "duplicate" for e in events), 2)
        self.assertEqual(self.runtime.dynamics, Dynamics())

    def test_wrong_claim_binding(self):
        def bad(*args):
            return (Evidence("wrong", "remainder", "fixture", Verdict.SUPPORTED, "not bound"),)
        self.assertEqual(self.search(v=bad).verdict, Verdict.INCONCLUSIVE)

    def test_bypass_blocked(self):
        self.assertEqual(self.search(p=lambda ctx: {**proposer(ctx), "verdict": "supported"}).verdict,
                         Verdict.INCONCLUSIVE)

    def test_dynamics_and_replay(self):
        observations = [Observation("synthetic", "event 1", "refuted", "false candidate"),
                        Observation("synthetic", "event 2", "duplicate", "repeated candidate")]
        trace = replay(observations)
        self.assertEqual(trace, replay(observations))
        self.assertEqual(trace[0]["after"], trace[1]["before"])
        self.assertGreater(trace[0]["after"]["surprise"], 0)
        self.assertGreater(trace[1]["after"]["stagnation"], 0)
        self.assertEqual(Dynamics().observe("refuted").policy((Strategy.EXPLORE,), adaptive=True),
                         Strategy.COUNTEREXAMPLE)

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.db")
            first = SearchRuntime(path)
            try:
                result = first.search("Find divisor", proposer, verifier,
                                      required_checks=frozenset({"remainder"}), budget=1)
                state = first.dynamics
            finally:
                first.close()
            second = SearchRuntime(path)
            try:
                self.assertEqual(second.dynamics, state)
                self.assertEqual(second.history(result.run_id)[-1]["kind"], "search_end")
            finally:
                second.close()

    def test_synthetic_ablation(self):
        report = compare()
        self.assertEqual(report["fixed"]["verdict"], "inconclusive")
        self.assertEqual(report["adaptive"]["verdict"], "supported")
        self.assertLessEqual(report["adaptive"]["attempts"], 4)


if __name__ == "__main__":
    unittest.main()
