"""Synthetic smoke benchmark, NOT empirical evidence about real LLMs."""
from behavioral import SearchRuntime, Strategy
from runtime_v2 import Evidence, Verdict, claim_id
import json


def proposer(ctx):
    phase = ctx["phase"]
    if phase == "decompose":
        return {"phase": phase, "subproblems": ["evaluate candidate", "check arithmetic"]}
    if phase == "hypothesize":
        # Deliberately strategy-sensitive fixture: tests wiring, not generalization.
        n = 41 if ctx["strategy"] == Strategy.COUNTEREXAMPLE.value else 37
        return {"phase": phase, "claim": f"1681 is divisible by {n}"}
    return {"phase": phase, "attacks": ["verify remainder", "verify quotient product"]}


def verifier(task, claim, strategy, attacks):
    candidates = {f"1681 is divisible by {n}": n for n in (37, 41)}
    if claim not in candidates:
        raise ValueError("unknown fixture claim")
    n = candidates[claim]
    q, r = divmod(1681, n)
    verdict = Verdict.SUPPORTED if r == 0 else Verdict.REFUTED
    return (Evidence(claim_id(task, claim), "remainder", "integer arithmetic", verdict,
                     f"1681 = {q} * {n} + {r}"),)


def compare(budget=4):
    results = {}
    for adaptive in (False, True):
        runtime = SearchRuntime()
        try:
            result = runtime.search("Find a nontrivial divisor of 1681", proposer, verifier,
                                    required_checks=frozenset({"remainder"}), budget=budget,
                                    adaptive=adaptive)
            events = runtime.history(result.run_id)
            results["adaptive" if adaptive else "fixed"] = {
                "verdict": result.verdict.value,
                "attempts": sum(e["kind"] == "strategy" for e in events),
                "verifications": len(result.candidates),
                "supported_claims": result.supported_claims,
                "strategy_trace": [e["data"]["strategy"] for e in events if e["kind"] == "strategy"],
            }
        finally:
            runtime.close()
    return results


if __name__ == "__main__":
    print(json.dumps({"scope": "synthetic controller wiring only", "results": compare()}, indent=2))
