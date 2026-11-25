# Appendix B Substrate

A dependency-free Python substrate for agent task loops that enforces:

**decompose → skeptical attack → independent verification → conclusion**

It carries continuous numerical emotional-state variables across turns and completed runs. These variables are explicit workflow state—not a claim of human feelings or access to a model's private reasoning.

## What it enforces

- Decomposition before an answer is attempted.
- Multiple skeptical attacks before verification.
- Independently computed checks rather than self-declared success.
- Failed verification returns the agent to the attack phase.
- Unverified conclusions are blocked.
- Emotional state persists across runs instead of resetting each turn.

## Run the demonstration

Requires Python 3.10 or newer. No third-party packages are needed.

```bash
python3 substrate.py
```

The demonstration tests a primality conjecture, finds a counterexample, prints independent checks and the emotional-state trajectory, and confirms that skipping directly to a conclusion is blocked.

## Scope

This substrate governs an agent's external workflow. It cannot guarantee that a language model's private reasoning follows the protocol, and it cannot prove arbitrary claims without appropriate independent verifiers.


## Experimental v0.2 runtime

The original `substrate.py` remains unchanged for baseline comparisons.
The new `runtime_v2.py` implements decomposition, explicit hypotheses,
state-dependent skeptical attacks, claim-bound evidence, and deterministic
conclusions with supported, refuted, or inconclusive verdicts.

```bash
python -m unittest -v test_runtime_v2
python substrate.py
```

Use `Runtime("state.db")` for durable SQLite state and run event history.
The caller supplies an agent callback, a trusted verifier callback, and
the required check identifiers. The verifier must establish actual evidence
and reject claims outside its domain; hashing a claim does not establish truth.
The runtime prevents agent-authored final prose from bypassing verdicts.
Distinct attack strings do not guarantee semantically independent attacks.
Conflicting evidence and callback errors produce inconclusive results.
The state-dependent attack budget is heuristic, not scientifically calibrated.
Attempt limits do not impose wall-clock timeouts on callbacks.
The runtime is synchronous, single-worker, and not a sandbox for untrusted Python.
SQLite records are not tamper-proof; interrupted runs can be inspected but
are not automatically resumed. Use a separate database per worker.
