"""Appendix B substrate quickstart: a wrong guess is refuted, a corrected one is verified."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from behavioral import SearchRuntime
from runtime_v2 import Evidence, Verdict, claim_id


def steps(n):
    s = 0
    while n != 1:
        n = n // 2 if n % 2 == 0 else 3 * n + 1
        s += 1
    return s


def proposer(ctx):                      # stands in for a model call
    if ctx["phase"] == "decompose":
        return {"phase": "decompose", "subproblems": ["define a step", "search n > 10,000"]}
    if ctx["phase"] == "hypothesize":   # first guess is wrong on purpose
        return {"phase": "hypothesize", "claim": "10971" if ctx["history"] else "10011"}
    return {"phase": "attack", "attacks": ["off-by-one in the count", "a smaller n might qualify"]}


def verifier(task, claim, strategy, attacks):   # trusted: computed, never asked
    n = int(claim)
    ok = steps(n) >= 260 and all(steps(m) < 260 for m in range(10001, n))
    verdict = Verdict.SUPPORTED if ok else Verdict.REFUTED
    return (Evidence(claim_id(task, claim), "exhaustive", "python", verdict, f"{n} takes {steps(n)} steps"),)


rt = SearchRuntime()                    # SearchRuntime("state.db") persists state across runs
result = rt.search("smallest n > 10,000 with >= 260 Collatz steps", proposer, verifier,
                   required_checks=frozenset({"exhaustive"}), budget=4)

for c in result.candidates:
    print(c.claim, c.verdict.value, "-", c.evidence[0].detail)
print("verdict:", result.verdict.value, result.supported_claims)
print("surprise after the refuted guess:", round(rt.dynamics.surprise, 2))
