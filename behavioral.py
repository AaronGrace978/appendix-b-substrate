"""Observable behavioral distillation: experimental controller, not private thought.

No dependencies. A trusted proposer supplies structured candidates via a callback;
a separate trusted verifier supplies claim-bound evidence. No model is embedded.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
import math
from typing import Mapping, Callable
from uuid import uuid4

from runtime_v2 import Runtime, Evidence, Verdict, claim_id, _strings


class Strategy(str, Enum):
    EXPLORE = "explore_alternative"
    COUNTEREXAMPLE = "search_counterexample"
    RECONSTRUCT = "independent_reconstruction"
    REDUCE = "reduce_to_subproblem"


@dataclass(frozen=True)
class Dynamics:
    curiosity: float = .65
    skepticism: float = .80
    confidence: float = .10  # confidence in resolution, NOT probability of claim truth
    resolve: float = .70
    strain: float = .05
    surprise: float = 0.
    stagnation: float = 0.

    def __post_init__(self):
        for value in asdict(self).values():
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("dynamics must be finite numbers in [0, 1]")

    def observe(self, event: str) -> Dynamics:
        # Coupled, explicitly heuristic event-driven update.
        if event not in ("supported", "refuted", "inconclusive", "duplicate"):
            raise ValueError("unknown event")
        failed = event in ("inconclusive", "duplicate")
        unexpected = event == "refuted"
        surprise = .65 * self.surprise + .35 * float(unexpected)
        stagnation = .7 * self.stagnation + .3 * float(failed)
        strain = .85 * self.strain + .15 * float(failed or unexpected)
        clamp = lambda x: min(1., max(0., x))
        return Dynamics(
            clamp(.9 * self.curiosity + .065 + .12 * surprise + .08 * stagnation),
            clamp(.9 * self.skepticism + .08 + .10 * surprise + .06 * stagnation),
            clamp(.85 * self.confidence + (.14 if not failed else -.05)),
            clamp(.95 * self.resolve + .035 + .04 * stagnation - .04 * strain),
            strain, surprise, stagnation,
        )

    def policy(self, history: tuple[Strategy, ...], *, adaptive: bool) -> Strategy:
        if not adaptive:
            return Strategy.EXPLORE
        order = (Strategy.REDUCE, Strategy.RECONSTRUCT, Strategy.COUNTEREXAMPLE, Strategy.EXPLORE) if self.strain >= .35 else (
            Strategy.COUNTEREXAMPLE, Strategy.RECONSTRUCT, Strategy.REDUCE, Strategy.EXPLORE) if self.surprise >= .2 else (
            Strategy.EXPLORE, Strategy.RECONSTRUCT, Strategy.COUNTEREXAMPLE, Strategy.REDUCE)
        # Don't repeat strategies until alternatives have been attempted.
        recent = history[-3:]
        return next((strategy for strategy in order if strategy not in recent), order[0])


@dataclass(frozen=True)
class Candidate:
    claim: str
    strategy: Strategy
    verdict: Verdict
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class SearchResult:
    run_id: str
    task: str
    verdict: Verdict
    candidates: tuple[Candidate, ...]
    reason: str

    @property
    def supported_claims(self) -> tuple[str, ...]:
        return tuple(c.claim for c in self.candidates if c.verdict is Verdict.SUPPORTED)


Proposer = Callable[[Mapping], Mapping]
Verifier = Callable[[str, str, Strategy, tuple[str, ...]], tuple[Evidence, ...]]


class SearchRuntime(Runtime):
    """Search for a supported candidate, or explicitly abstain.

    Refuting one candidate does not refute the original task or exhaust a domain.
    A supported candidate is relative to caller-defined check coverage.
    Single worker; no automatic resumption after interruption.
    """

    def __init__(self, path: str = ":memory:"):
        super().__init__(path)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS behavior_state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
        import json
        row = self.db.execute("SELECT data FROM behavior_state WHERE id=1").fetchone()
        self.dynamics = Dynamics(**json.loads(row[0])) if row else Dynamics()

    def _observation(self, run_id: str, event: str, data: dict, adaptive: bool):
        import json
        before = self.dynamics
        after = before.observe(event) if adaptive else before
        payload = {**data, "event": event, "before": asdict(before), "after": asdict(after)}
        with self.db:
            self.db.execute("INSERT INTO events(run_id,kind,data) VALUES(?,?,?)",
                            (run_id, "observation", json.dumps(payload, allow_nan=False)))
            if adaptive:
                self.db.execute("INSERT INTO behavior_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                                (json.dumps(asdict(after)),))
        self.dynamics = after

    def search(self, task: str, proposer: Proposer, verifier: Verifier, *,
               required_checks: frozenset[str], budget: int = 8,
               adaptive: bool = True) -> SearchResult:
        if type(task) is not str or not task.strip():
            raise ValueError("task required")
        if type(budget) is not int or not 1 <= budget <= 100:
            raise ValueError("budget must be 1..100")
        if not required_checks or any(type(x) is not str or not x.strip() for x in required_checks):
            raise ValueError("required check identifiers")
        required_checks = frozenset(required_checks)
        run_id = uuid4().hex
        candidates: list[Candidate] = []
        strategies: list[Strategy] = []
        seen: set[str] = set()
        verdict, reason = Verdict.INCONCLUSIVE, "search budget exhausted"
        self._event(run_id, "search_start", {"task": task, "budget": budget, "adaptive": adaptive,
                                            "required_checks": sorted(required_checks), "state": asdict(self.dynamics)})
        try:
            parts = _strings(self._step(proposer, "decompose", {"task": task}).get("subproblems"), 2)
            self._event(run_id, "decompose", {"subproblems": parts})
            for index in range(budget):
                strategy = self.dynamics.policy(tuple(strategies), adaptive=adaptive)
                strategies.append(strategy)
                ctx = {"task": task, "subproblems": parts, "strategy": strategy.value,
                       "attempt": index + 1, "state": asdict(self.dynamics),
                       "history": [asdict(c) for c in candidates]}
                self._event(run_id, "strategy", {"attempt": index+1, "strategy": strategy})
                claim = self._step(proposer, "hypothesize", ctx).get("claim")
                if type(claim) is not str or not claim.strip():
                    raise ValueError("candidate claim required")
                claim = claim.strip()
                key = " ".join(claim.casefold().split())
                if key in seen:
                    self._observation(run_id, "duplicate", {"claim": claim, "strategy": strategy}, adaptive)
                    continue
                seen.add(key)
                # Fixed minimum for both conditions: isolate the strategy controller.
                attacks = _strings(self._step(proposer, "attack", {**ctx, "claim": claim,
                                   "minimum_attacks": 2}).get("attacks"), 2)
                self._event(run_id, "candidate", {"claim": claim, "strategy": strategy, "attacks": attacks})
                evidence = verifier(task, claim, strategy, attacks)
                outcome = self._check(task, claim, evidence, required_checks)
                candidates.append(Candidate(claim, strategy, outcome, evidence))
                self._observation(run_id, outcome.value, {"claim": claim, "strategy": strategy,
                                  "evidence": [asdict(e) for e in evidence]}, adaptive)
                if outcome is Verdict.SUPPORTED:
                    verdict, reason = outcome, "candidate satisfies configured checks"
                    break
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self._event(run_id, "error", {"reason": reason})
        result = SearchResult(run_id, task, verdict, tuple(candidates), reason)
        self._event(run_id, "search_end", {"verdict": verdict, "reason": reason,
                                          "supported_claims": result.supported_claims})
        return result
