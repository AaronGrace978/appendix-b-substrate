import tempfile
import unittest
from pathlib import Path

from runtime_v2 import Runtime, Evidence, Verdict, claim_id, State


def agent(ctx):
    phase = ctx["phase"]
    response = {"phase": phase}
    if phase == "decompose":
        response["subproblems"] = ["evaluate polynomial", "check factors"]
    elif phase == "hypothesize":
        response["claim"] = "40**2 + 40 + 41 is prime"
    elif phase == "attack":
        response["attacks"] = [f"check candidate divisor {n}" for n in range(2, 2 + ctx["minimum_attacks"])]
    return response


def verifier(task, claim, attacks):
    if claim != "40**2 + 40 + 41 is prime":
        raise ValueError("unsupported claim")
    n = 40**2 + 40 + 41
    verdict = Verdict.REFUTED if n == 41*41 else Verdict.INCONCLUSIVE
    return (Evidence(claim_id(task, claim), "factor", "integer arithmetic", verdict, f"{n} = 41 * 41"),)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Runtime()
        self.addCleanup(self.runtime.close)

    def run_task(self, a=agent, v=verifier, **kwargs):
        return self.runtime.run("Test primality", a, v, required_checks=frozenset({"factor"}), **kwargs)

    def test_refutation_and_audit(self):
        result = self.run_task()
        self.assertEqual(result.verdict, Verdict.REFUTED)
        self.assertTrue(result.answer.startswith("REFUTED:"))
        self.assertEqual([e["kind"] for e in self.runtime.history(result.run_id)],
                         ["start", "decompose", "hypothesize", "attack", "verify", "conclude"])

    def test_skip(self):
        result = self.run_task(lambda _: {"phase": "conclude"})
        self.assertEqual(result.verdict, Verdict.INCONCLUSIVE)

    def test_answer_injection(self):
        result = self.run_task(lambda c: {**agent(c), "answer": "definitely prime"})
        self.assertEqual(result.verdict, Verdict.INCONCLUSIVE)

    def test_duplicate_attacks(self):
        def bad(c):
            return {"phase": "attack", "attacks": ["same"]*10} if c["phase"] == "attack" else agent(c)
        self.assertEqual(self.run_task(bad).verdict, Verdict.INCONCLUSIVE)

    def test_wrong_claim(self):
        def wrong(t, c, a):
            return (Evidence(claim_id(t, "unrelated"), "factor", "test", Verdict.SUPPORTED, "wrong"),)
        self.assertEqual(self.run_task(v=wrong).verdict, Verdict.INCONCLUSIVE)

    def test_missing_check(self):
        def missing(t, c, a):
            return (Evidence(claim_id(t,c), "unrelated", "test", Verdict.SUPPORTED, "wrong"),)
        self.assertEqual(self.run_task(v=missing).verdict, Verdict.INCONCLUSIVE)

    def test_conflicting_evidence(self):
        def conflict(t,c,a):
            return verifier(t,c,a) + (Evidence(claim_id(t,c), "other", "test", Verdict.SUPPORTED, "conflict"),)
        result = self.run_task(v=conflict, max_attempts=2)
        self.assertEqual(result.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(result.attempts, 2)

    def test_verifier_failure(self):
        def fail(*args):
            raise RuntimeError("unavailable")
        self.assertEqual(self.run_task(v=fail).verdict, Verdict.INCONCLUSIVE)

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.db")
            first = Runtime(path)
            result = first.run("Test primality", agent, verifier, required_checks=frozenset({"factor"}))
            state = first.state
            first.close()
            second = Runtime(path)
            try:
                self.assertEqual(second.state, state)
                self.assertEqual(second.history(result.run_id)[-1]["kind"], "conclude")
            finally:
                second.close()

    def test_adaptive_policy(self):
        self.assertGreater(State(skepticism=1).attack_count, State(skepticism=0).attack_count)
        with self.assertRaises(ValueError):
            State(confidence=float("nan"))


if __name__ == "__main__":
    unittest.main()
