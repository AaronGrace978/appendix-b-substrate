"""A dependency-free thinking/state substrate for agent task loops.

The substrate enforces an external Appendix-B-style protocol:
    decompose -> attack -> verify -> conclude

It cannot inspect a model's private thoughts. It can prevent an agent from
claiming completion without decomposition, skeptical attacks, and independent
checks. EmotionalState is deliberately continuous across turns and runs.

Run this file directly for a real, self-contained demonstration:
    python3 substrate.py
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Any, Callable, Iterable, Mapping


class Phase(str, Enum):
    DECOMPOSE = "decompose"
    ATTACK = "attack"
    VERIFY = "verify"
    CONCLUDE = "conclude"


class ProtocolError(RuntimeError):
    """Raised when an agent tries to skip a gate or makes a bad conclusion."""


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


@dataclass
class EmotionalState:
    """Small continuous state variables, not a claim of human feelings."""

    curiosity: float = 0.65
    skepticism: float = 0.80
    confidence: float = 0.10
    resolve: float = 0.70
    strain: float = 0.05

    def __post_init__(self) -> None:
        self._normalize()

    def _normalize(self) -> None:
        for name in ("curiosity", "skepticism", "confidence", "resolve", "strain"):
            setattr(self, name, _clamp(getattr(self, name)))

    def advance(self) -> None:
        """Carry state forward with gentle mean reversion; never reset per turn."""
        baselines = {
            "curiosity": 0.65,
            "skepticism": 0.80,
            "confidence": 0.10,
            "resolve": 0.70,
            "strain": 0.05,
        }
        for name, baseline in baselines.items():
            value = getattr(self, name)
            setattr(self, name, value + (baseline - value) * 0.04)

    def observe(self, event: str, success: bool = True) -> None:
        """Update state from accepted protocol events, including failed checks."""
        if event == "decompose":
            self.curiosity += 0.04
            self.resolve += 0.01
        elif event == "attack":
            self.skepticism += 0.03
            self.curiosity += 0.02
        elif event == "verify":
            if success:
                self.confidence += 0.18
                self.strain -= 0.04
            else:
                self.confidence -= 0.12
                self.skepticism += 0.05
                self.strain += 0.08
        elif event == "conclude":
            self.confidence += 0.05
            self.resolve += 0.02
        self._normalize()

    def as_dict(self) -> dict[str, float]:
        return {
            name: round(getattr(self, name), 4)
            for name in ("curiosity", "skepticism", "confidence", "resolve", "strain")
        }


@dataclass(frozen=True)
class Check:
    """An independently computed verification result, not an agent self-claim."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Verification:
    claim: str
    checks: tuple[Check, ...]
    # False can mean a verified falsification; ok describes check execution.
    claim_supported: bool | None = None
    negative_control: Check | None = None

    @property
    def ok(self) -> bool:
        return (bool(self.checks) and all(check.passed for check in self.checks)
                and self.negative_control is not None and self.negative_control.passed is True)


@dataclass(frozen=True)
class Turn:
    number: int
    phase: Phase
    payload: Mapping[str, Any]
    emotion: dict[str, float]


@dataclass(frozen=True)
class RunResult:
    task: str
    answer: str
    turns: tuple[Turn, ...]
    verification: Verification
    emotional_state: dict[str, float]

    @property
    def verified(self) -> bool:
        return self.verification.ok


Agent = Callable[[dict[str, Any]], Mapping[str, Any]]
Verifier = Callable[[str, str, Mapping[str, Any]], Verification | Iterable[Check]]


class ThinkingSubstrate:
    """Drive an agent through gated phases while retaining emotional state."""

    def __init__(self, state: EmotionalState | None = None) -> None:
        self.state = state or EmotionalState()
        self.turns: list[Turn] = []
        self.completed_runs = 0
        self._reset_run()

    def _reset_run(self) -> None:
        self._phase = Phase.DECOMPOSE
        self._decomposition: tuple[str, ...] = ()
        self._attacks: tuple[str, ...] = ()
        self._verification: Verification | None = None
        self.turns = []

    @property
    def required_phase(self) -> Phase:
        return self._phase

    @staticmethod
    def _strings(payload: Mapping[str, Any], key: str, minimum: int = 2) -> tuple[str, ...]:
        value = payload.get(key)
        if not isinstance(value, (list, tuple)):
            raise ProtocolError(f"{key} must be a list of at least {minimum} strings")
        items = tuple(str(item).strip() for item in value if str(item).strip())
        if len(items) < minimum:
            raise ProtocolError(f"{key} needs at least {minimum} non-empty entries")
        return items

    def _context(self, task: str) -> dict[str, Any]:
        return {
            "task": task,
            "phase": self._phase.value,
            "turn": len(self.turns) + 1,
            "emotional_state": self.state.as_dict(),
            "decomposition": self._decomposition,
            "attacks": self._attacks,
            "verification": self._verification,
        }

    def _record(self, payload: Mapping[str, Any]) -> None:
        self.turns.append(Turn(len(self.turns) + 1, self._phase, dict(payload), self.state.as_dict()))

    def run(self, task: str, agent: Agent, verifier: Verifier | None = None, *, max_turns: int = 12) -> RunResult:
        """Run an agent callback until a verified conclusion or a protocol error.

        Each callback receives the current required phase and state. It must
        return {phase: <required phase>, payload: {...}}. A verifier is called
        only at the verify gate; the agent cannot manufacture its own evidence.
        """
        if not task.strip():
            raise ValueError("task must not be empty")
        if max_turns < 4:
            raise ValueError("max_turns must allow the four protocol phases")
        self._reset_run()  # Reset task progress, never the continuous state.

        for _ in range(max_turns):
            context = self._context(task)
            step = agent(context)
            if not isinstance(step, Mapping):
                raise ProtocolError("agent must return a mapping")
            try:
                phase = Phase(step.get("phase"))
            except (TypeError, ValueError) as exc:
                raise ProtocolError("agent returned an unknown or missing phase") from exc
            if phase != self._phase:
                raise ProtocolError(f"phase gate: required {self._phase.value}, got {phase.value}")
            if step.get("done") and phase != Phase.CONCLUDE:
                raise ProtocolError("done is forbidden before a verified conclusion")
            payload = step.get("payload", {})
            if not isinstance(payload, Mapping):
                raise ProtocolError("payload must be a mapping")
            self.state.advance()

            if phase == Phase.DECOMPOSE:
                self._decomposition = self._strings(payload, "subproblems")
                self.state.observe("decompose")
                self._record(payload)
                self._phase = Phase.ATTACK
            elif phase == Phase.ATTACK:
                self._attacks = self._strings(payload, "attacks")
                self.state.observe("attack")
                self._record(payload)
                self._phase = Phase.VERIFY
            elif phase == Phase.VERIFY:
                if verifier is None:
                    raise ProtocolError("an independent verifier is required")
                claim = str(payload.get("claim", "")).strip()
                if not claim:
                    raise ProtocolError("verify requires a concrete claim")
                raw_report = verifier(task, claim, payload)
                if isinstance(raw_report, Verification):
                    report = Verification(claim, tuple(raw_report.checks), raw_report.claim_supported,
                                          raw_report.negative_control)
                else:
                    report = Verification(claim, tuple(raw_report))
                if not report.checks:
                    raise ProtocolError("verification must contain independent checks")
                self._verification = report
                self.state.observe("verify", report.ok)
                self._record(payload)
                # Failed checks send the loop back to skeptical attack.
                self._phase = Phase.CONCLUDE if report.ok else Phase.ATTACK
            else:  # CONCLUDE
                if self._verification is None or not self._verification.ok:
                    raise ProtocolError("no conclusion without passing verification")
                answer = str(payload.get("answer", "")).strip()
                if not answer:
                    raise ProtocolError("conclusion requires a non-empty answer")
                expected = self._verification.claim_supported
                if expected is not None:
                    if not isinstance(payload.get("claim_supported"), bool):
                        raise ProtocolError("conclusion must explicitly state claim_supported")
                    if payload["claim_supported"] != expected:
                        raise ProtocolError("conclusion contradicts independent verification")
                self.state.observe("conclude")
                self._record(payload)
                self.completed_runs += 1
                return RunResult(task, answer, tuple(self.turns), self._verification, self.state.as_dict())

        raise ProtocolError("turn budget exhausted before a verified conclusion")


# ----------------------------- runnable demonstration -----------------------------

_DEMO_CLAIM = "For every integer n from 0 through 40, n^2 + n + 41 is prime."


def _is_prime(value: int) -> bool:
    if value < 2:
        return False
    if value % 2 == 0:
        return value == 2
    return all(value % divisor for divisor in range(3, math.isqrt(value) + 1, 2))


def _demo_agent(context: dict[str, Any]) -> Mapping[str, Any]:
    phase = context["phase"]
    if phase == Phase.DECOMPOSE.value:
        return {"phase": phase, "payload": {"subproblems": [
            "Enumerate every n in the stated domain and evaluate the polynomial.",
            "Test each resulting integer with a deterministic primality procedure.",
            "Search for and independently reproduce any counterexample.",
        ]}}
    if phase == Phase.ATTACK.value:
        return {"phase": phase, "payload": {"attacks": [
            "Small prime outputs are not evidence for a universal claim; test the whole domain.",
            "Check the primality procedure against known primes and composites before trusting it.",
            "A single reproducible divisor falsifies the claim; do not explain it away.",
        ]}}
    if phase == Phase.VERIFY.value:
        return {"phase": phase, "payload": {"claim": _DEMO_CLAIM, "domain": list(range(41))}}
    if phase == Phase.CONCLUDE.value:
        supported = context["verification"].claim_supported
        answer = (
            "The conjecture is false: n=40 gives 1681 = 41 x 41. "
            "The earlier prime values do not establish a universal claim."
            if not supported else
            "The conjecture survived the exhaustive, independently checked domain."
        )
        return {"phase": phase, "payload": {"answer": answer, "claim_supported": supported}}
    raise AssertionError(phase)


def _demo_verifier(task: str, claim: str, payload: Mapping[str, Any]) -> Verification:
    domain = payload.get("domain", [])
    values = [(n, n * n + n + 41) for n in domain]
    counterexamples = [(n, value) for n, value in values if not _is_prime(value)]
    witnesses = []
    for n, value in counterexamples:
        divisor = next((d for d in range(2, math.isqrt(value) + 1) if value % d == 0), None)
        witnesses.append((n, value, divisor))
    known_primes = (2, 3, 41)
    witness_text = "; ".join(
        f"n={n}: {value} = {divisor} x {value // divisor}" if divisor else f"n={n}: no divisor"
        for n, value, divisor in witnesses
    ) or "no counterexample found"
    checks = (
        Check("domain-exhausted", domain == list(range(41)) and len(values) == 41,
              f"evaluated {len(values)} domain points (0 through 40)"),
        Check("known-primes", all(_is_prime(number) for number in known_primes),
              "2, 3 and 41 should be classified as prime"),
        Check("counterexample-witnesses", all(divisor is not None and value % divisor == 0
              for _, value, divisor in witnesses), witness_text),
    )
    negative_control = Check("known-composites", all(not _is_prime(n) for n in (4, 9, 169)),
                             "4, 9 and 169 must be rejected as primes")
    return Verification(claim, checks, claim_supported=not counterexamples, negative_control=negative_control)


def main() -> None:
    substrate = ThinkingSubstrate()
    result = substrate.run("Test the polynomial primality conjecture.", _demo_agent, _demo_verifier)
    print("APPENDIX B SUBSTRATE DEMO")
    print("protocol: decompose -> attack -> verify -> conclude")
    for turn in result.turns:
        print(f"turn {turn.number}: {turn.phase.value}; emotion={turn.emotion}")
    print("independent checks:")
    for check in (*result.verification.checks, result.verification.negative_control):
        print(f"  {'PASS' if check.passed else 'FAIL'} {check.name}: {check.detail}")
    print(f"verified={result.verified}; claim_supported={result.verification.claim_supported}")
    print(f"answer={result.answer}")
    print(f"final_emotion={result.emotional_state}")

    # Prove that an agent cannot skip directly to an unverified answer.
    gate = ThinkingSubstrate()
    try:
        gate.run("skip the protocol", lambda context: {
            "phase": "conclude", "payload": {"answer": "unverified"}
        }, _demo_verifier)
    except ProtocolError as error:
        print(f"gate_test=BLOCKED: {error}")
    else:
        raise AssertionError("phase gate failed to block an unverified conclusion")


if __name__ == "__main__":
    main()
