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
