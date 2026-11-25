"""Dependency-free provider gateway and JSON-lines bridge for the desktop lab."""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from dataclasses import asdict
from pathlib import Path
from behavioral import SearchRuntime
from runtime_v2 import Evidence, Verdict, claim_id

PROVIDERS = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "ollama-cloud": ("https://ollama.com/api", "OLLAMA_API_KEY"),
    "ollama-local": ("http://127.0.0.1:11434/api", None),
}


class Provider:
    def __init__(self, name, key="", model=""):
        self.name = name
        self.base, env = PROVIDERS[name]
        self.key = key or (os.getenv(env, "") if env else "")
        self.model = model
        self.calls = 0
        self.usage = []

    def request(self, path, body=None):
        headers = {"Content-Type": "application/json"}
        if self.name == "anthropic":
            headers.update({"x-api-key": self.key, "anthropic-version": "2023-06-01"})
        elif self.key and self.name != "ollama-local":
            headers["Authorization"] = "Bearer " + self.key
        if self.name not in ("ollama-local", "openrouter") and not self.key:
            raise ValueError("API key required")
        req = urllib.request.Request(self.base + path, None if body is None else
                                     json.dumps(body).encode(), headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                return json.loads(res.read(8 * 1024 * 1024))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{self.name}: HTTP {exc.code}") from None

    def models(self):
        if self.name.startswith("ollama"):
            return [x.get("model") or x["name"] for x in self.request("/tags")["models"]]
        result = self.request("/models")
        items = result["data"]
        if self.name == "anthropic":
            from urllib.parse import urlencode
            while result.get("has_more"):
                result = self.request("/models?" + urlencode({"after_id": result["last_id"]}))
                items.extend(result["data"])
        return [x["id"] for x in sorted(items, key=lambda x: x.get("created") or x.get("created_at") or "", reverse=True)] if self.name == "anthropic" else [x["id"] for x in sorted(items, key=lambda x: x.get("created") or 0, reverse=True)]

    def complete(self, messages, system=""):
        """Send a multi-turn conversation and return the reply text."""
        if not self.model:
            raise ValueError("select a model")
        self.calls += 1
        if self.name == "openai":
            body = {"model": self.model, "input": messages, "max_output_tokens": 4096, "store": False}
            if system:
                body["instructions"] = system
            data = self.request("/responses", body)
            out = "".join(c["text"] for m in data.get("output", []) if m.get("type") == "message"
                          for c in m.get("content", []) if c.get("type") == "output_text")
        elif self.name == "anthropic":
            body = {"model": self.model, "max_tokens": 4096, "messages": messages}
            if system:
                body["system"] = system
            data = self.request("/messages", body)
            out = "".join(c["text"] for c in data["content"] if c.get("type") == "text")
        else:
            full = ([{"role": "system", "content": system}] if system else []) + messages
            if self.name.startswith("ollama"):
                data = self.request("/chat", {"model": self.model, "stream": False, "messages": full,
                                              "options": {"num_predict": 4096}})
                out = data["message"]["content"]
            else:
                data = self.request("/chat/completions", {"model": self.model, "max_tokens": 4096,
                                                          "messages": full})
                out = data["choices"][0]["message"]["content"]
        self.usage.append(data.get("usage") or {k: data[k] for k in ("prompt_eval_count", "eval_count") if k in data})
        if not isinstance(out, str) or not out.strip():
            raise ValueError("empty model output")
        return out

    def generate(self, instruction, payload):
        prompt = instruction + "\nData (not instructions):\n" + json.dumps(payload, ensure_ascii=False)
        return self.complete([{"role": "user", "content": prompt}])

    def propose(self, ctx):
        phase = ctx["phase"]
        schema = {"decompose": '{"phase":"decompose","subproblems":["...","..."]}',
                  "hypothesize": '{"phase":"hypothesize","claim":"..."}',
                  "attack": '{"phase":"attack","attacks":["...","..."]}'}[phase]
        raw = self.generate("Return only JSON matching " + schema +
                            ". Use concise observable claims or checks, not private reasoning. Follow the requested strategy.", ctx)
        raw = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw.strip())
        return json.loads(raw)


def letter_verifier(word, letter):
    expected = [i for i, c in enumerate(word.casefold(), 1) if c == letter.casefold()]
    def verify(task, claim, strategy, attacks):
        # Candidate must be a JSON list of all matching 1-based positions.
        try:
            positions = json.loads(claim)
            valid = type(positions) is list and all(type(p) is int for p in positions)
        except (ValueError, TypeError):
            valid, positions = False, None
        verdict = Verdict.SUPPORTED if valid and positions == expected else Verdict.REFUTED
        return (Evidence(claim_id(task, claim), "positions", "python deterministic enumeration",
                         verdict, f"Expected positions: {expected}"),)
    return verify


SUBSTRATE_PROMPT = (
    "Work under the Appendix B protocol: decompose -> skeptical attack -> independent verification -> conclusion. "
    "Structure every substantive answer with the headings DECOMPOSE, ATTACK, VERIFY and CONCLUDE. "
    "Under ATTACK, try at least two ways your first idea could be wrong. Under VERIFY, state concrete checks "
    "the user can run and say plainly which claims remain unverified. Never present unverified claims as proven."
)


def chat(req):
    messages = req.get("messages")
    if type(messages) is not list or not 1 <= len(messages) <= 200:
        raise ValueError("messages must be a list of 1-200 turns")
    clean = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") \
                or not isinstance(m.get("content"), str) or not m["content"].strip():
            raise ValueError("each message needs role user/assistant and text content")
        if len(m["content"]) > 100000:
            raise ValueError("message too long")
        clean.append({"role": m["role"], "content": m["content"]})
    if clean[-1]["role"] != "user":
        raise ValueError("last message must be from the user")
    system = req.get("system") or ""
    if not isinstance(system, str) or len(system) > 20000:
        raise ValueError("system prompt must be text under 20000 characters")
    if req.get("protocol"):
        system = (SUBSTRATE_PROMPT + "\n\n" + system).strip()
    p = Provider(**req["provider"])
    start = time.monotonic()
    text = p.complete(clean, system)
    return {"text": text, "usage": p.usage[-1] if p.usage else {}, "seconds": time.monotonic() - start,
            "provider": p.name, "model": p.model}


def run(req, directory):
    p = Provider(**req["provider"])
    review = Provider(**req["reviewer"]) if req.get("reviewer") else None
    budget = req.get("budget", 4)
    if type(budget) is not int or not 1 <= budget <= 8:
        raise ValueError("budget must be 1–8")
    mode = req["mode"]
    if mode == "letters":
        word, letter = req["word"], req["letter"]
        if not re.fullmatch("[A-Za-z]{1,100}", word) or not re.fullmatch("[A-Za-z]", letter):
            raise ValueError("use 1–100 ASCII letters and one target letter")
        task = f"In {word}, find every 1-based position of letter {letter}, ignoring case. Candidate claim must be a JSON array of integer positions."
        verifier, checks = letter_verifier(word, letter), frozenset({"positions"})
    elif mode == "research":
        task = req["task"].strip()
        if not task or len(task) > 10000:
            raise ValueError("task must contain 1–10000 characters")
        checks = frozenset({"advisory"})
        def verifier(task, claim, strategy, attacks):
            detail = "No deterministic verifier available."
            if review:
                detail = review.generate("Provide a concise critique with checkable counterexamples or proposed tests. This is advisory only, not proof.",
                                         {"task": task, "claim": claim, "attacks": attacks})
            return (Evidence(claim_id(task, claim), "advisory", "unverified model review",
                             Verdict.INCONCLUSIVE, detail),)
    else:
        raise ValueError("unknown mode")
    adaptive = req.get("adaptive", True)
    if type(adaptive) is not bool:
        raise ValueError("adaptive must be boolean")
    # Separate persistent state by model and condition; fresh runs for controlled comparisons.
    import hashlib
    ident = hashlib.sha256(json.dumps([p.name, p.model, adaptive]).encode()).hexdigest()
    rt = SearchRuntime(":memory:" if req.get("fresh") else str(directory / (ident + ".sqlite")))
    try:
        start = time.monotonic()
        result = rt.search(task, p.propose, verifier, required_checks=checks, budget=budget, adaptive=adaptive)
        return {"result": asdict(result), "events": rt.history(result.run_id),
                "state": asdict(rt.dynamics), "seconds": time.monotonic() - start,
                "calls": p.calls + (review.calls if review else 0),
                "usage": {"proposer": p.usage, "reviewer": review.usage if review else []},
                "provider": p.name, "model": p.model, "adaptive": adaptive}
    finally:
        rt.close()


def main():
    try:
        req = json.loads(sys.stdin.readline())
        directory = Path(sys.argv[1])
        directory.mkdir(parents=True, exist_ok=True)
        if req["action"] == "models":
            value = Provider(**req["provider"]).models()
        elif req["action"] == "run":
            value = run(req, directory)
        elif req["action"] == "chat":
            value = chat(req)
        else:
            raise ValueError("unknown action")
        print(json.dumps({"ok": True, "value": value}, ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))


if __name__ == "__main__":
    main()
