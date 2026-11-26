"""Appendix B v0.2: evidence-bound reasoning, with no third-party dependencies.

The caller chooses the trusted verifier. Agents supply hypotheses and attacks,
never verdicts or final prose. A verifier is a trust boundary, not an oracle.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
import hashlib
import json
import math
import sqlite3
from typing import Callable, Mapping
from uuid import uuid4


class Verdict(str, Enum):
    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class Evidence:
    claim_id: str
    check_id: str
    source: str
    verdict: Verdict
    detail: str


@dataclass(frozen=True)
class State:
    curiosity: float = .65
    skepticism: float = .80
    confidence: float = .10
    resolve: float = .70
    strain: float = .05

    def __post_init__(self):
        for value in asdict(self).values():
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("state must contain finite numbers in [0, 1]")

    def observe(self, verdict: Verdict) -> State:
        resolved = verdict is not Verdict.INCONCLUSIVE
        # Heuristic control state, not a calibrated probability.
        return State(
            curiosity=min(1., self.curiosity + (.01 if resolved else .03)),
            skepticism=max(.2, self.skepticism - .01) if resolved else min(1., self.skepticism + .05),
            confidence=min(1., self.confidence + .10) if resolved else max(0., self.confidence - .12),
            resolve=min(1., self.resolve + .01),
            strain=max(0., self.strain - .04) if resolved else min(1., self.strain + .08),
        )

    @property
    def attack_count(self) -> int:
        return 2 + int(self.skepticism * 3)


@dataclass(frozen=True)
class Result:
    run_id: str
    task: str
    claim: str
    verdict: Verdict
    evidence: tuple[Evidence, ...]
    attempts: int
    reason: str

    @property
    def answer(self) -> str:
        # No free-form agent conclusion can bypass the verifier.
        return f"{self.verdict.value.upper()}: {self.claim or '(no candidate claim)'}"


def claim_id(task: str, claim: str) -> str:
    return hashlib.sha256(json.dumps([task, claim], ensure_ascii=False).encode()).hexdigest()


def _strings(value: object, minimum: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("expected a list of strings")
    if any(type(item) is not str or not item.strip() for item in value):
        raise ValueError("entries must be nonempty strings")
    items = tuple(item.strip() for item in value)
    if len({item.casefold() for item in items}) != len(items) or len(items) < minimum:
        raise ValueError("insufficient distinct entries")
    return items


Agent = Callable[[Mapping], Mapping]
Verifier = Callable[[str, str, tuple[str, ...]], tuple[Evidence, ...]]


class Runtime:
    """Bounded synchronous runtime. Use one instance per worker.

    SQLite stores an atomic state snapshot and append-only run events through
    this API. The database is not tamper-proof. Use separate DBs for unrelated
    agents and do not share a database between concurrent runtimes.
    """

    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL)")
        row = self.db.execute("SELECT data FROM state WHERE id=1").fetchone()
        self.state = State(**json.loads(row[0])) if row else State()

    def close(self):
        self.db.close()

    def _event(self, run_id: str, kind: str, data: dict, state: State | None = None):
        with self.db:
            self.db.execute("INSERT INTO events(run_id,kind,data) VALUES(?,?,?)",
                            (run_id, kind, json.dumps(data, allow_nan=False)))
            if state is not None:
                self.db.execute("INSERT INTO state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                                (json.dumps(asdict(state)),))
        if state is not None:
            self.state = state

    def history(self, run_id: str) -> list[dict]:
        rows = self.db.execute("SELECT kind,data FROM events WHERE run_id=? ORDER BY seq", (run_id,))
        return [{"kind": kind, "data": json.loads(data)} for kind, data in rows]

    @staticmethod
    def _step(agent: Agent, phase: str, context: dict) -> Mapping:
        step = agent({"phase": phase, **context})
        if not isinstance(step, Mapping) or step.get("phase") != phase:
            raise ValueError("invalid phase response")
        if "done" in step or "answer" in step or "verdict" in step:
            raise ValueError("agent cannot declare completion or verdict")
        return step

    @staticmethod
    def _check(task: str, claim: str, evidence: tuple[Evidence, ...], required: frozenset[str]) -> Verdict:
        if type(evidence) is not tuple or not evidence:
            raise ValueError("missing verification evidence")
        seen = set()
        for item in evidence:
            if not isinstance(item, Evidence) or item.claim_id != claim_id(task, claim):
                raise ValueError("evidence does not match task and claim")
            if any(type(s) is not str or not s.strip() for s in (item.check_id, item.source, item.detail)):
                raise ValueError("invalid evidence metadata")
            if item.check_id in seen or not isinstance(item.verdict, Verdict):
                raise ValueError("duplicate check or invalid verdict")
            seen.add(item.check_id)
        if not required.issubset(seen):
            raise ValueError("missing required checks")
        outcomes = {item.verdict for item in evidence}
        return outcomes.pop() if len(outcomes) == 1 else Verdict.INCONCLUSIVE

    def _verified_outcome(
        self, task: str, claim: str, evidence: tuple[Evidence, ...], required: frozenset[str],
        negative_control: str | None, verify: Callable[[str], tuple[Evidence, ...]],
    ) -> tuple[Verdict, dict | None, str]:
        outcome = self._check(task, claim, evidence, required)
        if outcome is Verdict.INCONCLUSIVE:
            return outcome, None, "required checks were inconclusive"
        if type(negative_control) is not str or not negative_control.strip():
            return Verdict.INCONCLUSIVE, None, "a negative control is required"
        if negative_control.strip() == claim:
            return Verdict.INCONCLUSIVE, None, "the negative control must differ from the candidate"
        control_evidence = verify(negative_control)
        control_outcome = self._check(task, negative_control, control_evidence, required)
        control = {"claim": negative_control, "verdict": control_outcome,
                   "evidence": [asdict(item) for item in control_evidence]}
        if control_outcome is not Verdict.REFUTED:
            return Verdict.INCONCLUSIVE, control, "verifier did not refute the negative control"
        return outcome, control, "required checks agree and negative control was refuted"

    def run(self, task: str, agent: Agent, verifier: Verifier, *,
            required_checks: frozenset[str], max_attempts: int = 3,
            negative_control: str | None = None) -> Result:
        if type(task) is not str or not task.strip():
            raise ValueError("task must be nonempty")
        if (type(max_attempts) is not int or not 1 <= max_attempts <= 100
                or not required_checks or any(type(x) is not str or not x.strip() for x in required_checks)):
            raise ValueError("invalid attempt budget or required checks")
        required_checks = frozenset(required_checks)
        run_id = uuid4().hex
        self._event(run_id, "start", {"task": task, "required_checks": sorted(required_checks)})
        claim, evidence, attempt = "", (), 0
        verdict, reason = Verdict.INCONCLUSIVE, "attempt budget exhausted"
        try:
            context = {"task": task, "state": asdict(self.state)}
            parts = _strings(self._step(agent, "decompose", context).get("subproblems"), 2)
            self._event(run_id, "decompose", {"subproblems": parts})
            for attempt in range(1, max_attempts + 1):
                context = {"task": task, "state": asdict(self.state), "subproblems": parts,
                           "previous_claim": claim, "previous_evidence": [asdict(e) for e in evidence]}
                candidate = self._step(agent, "hypothesize", context).get("claim")
                if type(candidate) is not str or not candidate.strip():
                    raise ValueError("missing candidate claim")
                claim, evidence = candidate.strip(), ()
                self._event(run_id, "hypothesize", {"claim": claim, "claim_id": claim_id(task, claim)})
                count = self.state.attack_count
                attacks = _strings(self._step(agent, "attack", {**context, "claim": claim,
                                   "minimum_attacks": count}).get("attacks"), count)
                self._event(run_id, "attack", {"claim_id": claim_id(task, claim), "attacks": attacks})
                report = verifier(task, claim, attacks)
                verdict, control, reason = self._verified_outcome(
                    task, claim, report, required_checks, negative_control,
                    lambda control_claim: verifier(task, control_claim, ()))
                evidence = report
                self._event(run_id, "verify", {"verdict": verdict, "evidence": [asdict(e) for e in evidence],
                                               "negative_control": control, "reason": reason},
                            self.state.observe(verdict))
                if verdict is not Verdict.INCONCLUSIVE:
                    break
        except Exception as exc:
            verdict, evidence = Verdict.INCONCLUSIVE, ()
            reason = f"{type(exc).__name__}: {exc}"
            self._event(run_id, "error", {"reason": reason})
        result = Result(run_id, task, claim, verdict, evidence, attempt, reason)
        self._event(run_id, "conclude", {"claim": claim, "verdict": verdict, "reason": reason,
                                        "attempts": attempt, "answer": result.answer})
        return result
