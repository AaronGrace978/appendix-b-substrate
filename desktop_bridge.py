"""Dependency-free provider gateway and JSON-lines bridge for the desktop lab."""
import json
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
from dataclasses import asdict
from pathlib import Path
from behavioral import SearchRuntime
from chat_checks import extract_checks
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

    def _stream_lines(self, path, body, cancelled):
        """Yield decoded text lines from a streaming HTTP response."""
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.name == "anthropic":
            headers.update({"x-api-key": self.key, "anthropic-version": "2023-06-01"})
        elif self.key and self.name != "ollama-local":
            headers["Authorization"] = "Bearer " + self.key
        if self.name not in ("ollama-local", "openrouter") and not self.key:
            raise ValueError("API key required")
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(), headers)
        try:
            res = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                err = json.loads(exc.read(65536))
                detail = (err.get("error") or {}).get("message", "") if isinstance(err.get("error"), dict) else str(err.get("error", ""))
            except Exception:
                pass
            raise RuntimeError(f"{self.name}: HTTP {exc.code}" + (f" - {detail[:300]}" if detail else "")) from None
        with res:
            for raw in res:
                if cancelled():
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line:
                    yield line

    def stream(self, messages, system="", on_delta=lambda s: None, cancelled=lambda: False):
        """Stream a multi-turn reply, calling on_delta with each text fragment."""
        if not self.model:
            raise ValueError("select a model")
        self.calls += 1
        parts, usage = [], {}

        def sse(lines):
            for line in lines:
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        yield json.loads(data)
                    except ValueError:
                        continue

        def emit(text):
            if text:
                parts.append(text)
                on_delta(text)

        if self.name == "openai":
            body = {"model": self.model, "input": messages, "max_output_tokens": 4096, "store": False, "stream": True}
            if system:
                body["instructions"] = system
            for ev in sse(self._stream_lines("/responses", body, cancelled)):
                if ev.get("type") == "response.output_text.delta":
                    emit(ev.get("delta", ""))
                elif ev.get("type") == "response.completed":
                    usage = (ev.get("response") or {}).get("usage") or {}
                elif ev.get("type") in ("error", "response.failed"):
                    raise RuntimeError("openai: " + json.dumps(ev.get("error") or ev.get("response", {}).get("error"))[:300])
        elif self.name == "anthropic":
            body = {"model": self.model, "max_tokens": 4096, "messages": messages, "stream": True}
            if system:
                body["system"] = system
            for ev in sse(self._stream_lines("/messages", body, cancelled)):
                t = ev.get("type")
                if t == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                    emit(ev["delta"]["text"])
                elif t == "message_start":
                    usage.update((ev.get("message") or {}).get("usage") or {})
                elif t == "message_delta":
                    usage.update(ev.get("usage") or {})
                elif t == "error":
                    raise RuntimeError("anthropic: " + str((ev.get("error") or {}).get("message", ""))[:300])
        elif self.name.startswith("ollama"):
            full = ([{"role": "system", "content": system}] if system else []) + messages
            body = {"model": self.model, "stream": True, "messages": full, "options": {"num_predict": 4096}}
            for line in self._stream_lines("/chat", body, cancelled):
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("error"):
                    raise RuntimeError(f"{self.name}: {str(ev['error'])[:300]}")
                emit((ev.get("message") or {}).get("content", ""))
                if ev.get("done"):
                    usage = {k: ev[k] for k in ("prompt_eval_count", "eval_count") if k in ev}
        else:
            full = ([{"role": "system", "content": system}] if system else []) + messages
            body = {"model": self.model, "max_tokens": 4096, "messages": full, "stream": True}
            for ev in sse(self._stream_lines("/chat/completions", body, cancelled)):
                if ev.get("error"):
                    raise RuntimeError("openrouter: " + str(ev["error"])[:300])
                for choice in ev.get("choices") or []:
                    emit((choice.get("delta") or {}).get("content") or "")
                if ev.get("usage"):
                    usage = ev["usage"]
        self.usage.append(usage)
        text = "".join(parts)
        if not text.strip() and not cancelled():
            raise ValueError("empty model output")
        return text

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


def chat(req, directory=None, on_delta=None, cancelled=lambda: False):
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
    if on_delta is None:
        text = p.complete(clean, system)
    else:
        text = p.stream(clean, system, on_delta, cancelled)
    checks = extract_checks(text) if req.get("verify", True) else []
    state = None
    if directory is not None and checks:
        state = observe_checks(directory, p, checks)
    return {"text": text, "usage": p.usage[-1] if p.usage else {}, "seconds": time.monotonic() - start,
            "provider": p.name, "model": p.model, "checks": checks, "state": state,
            "cancelled": bool(cancelled())}


def agent_store(directory, p):
    import hashlib
    ident = hashlib.sha256(json.dumps(["chat", p.name, p.model]).encode()).hexdigest()[:24]
    return SearchRuntime(str(Path(directory) / f"chat-{ident}.sqlite"))


def observe_checks(directory, p, checks):
    """Feed independent check outcomes into this model's persistent substrate state."""
    rt = agent_store(directory, p)
    try:
        from uuid import uuid4
        run_id = uuid4().hex
        rt._event(run_id, "chat_turn", {"provider": p.name, "model": p.model, "checks": len(checks)})
        for c in checks:
            rt._observation(run_id, c["verdict"], {"claim": c["claim"], "kind": c["kind"],
                                                   "detail": c["detail"]}, True)
        return asdict(rt.dynamics)
    finally:
        rt.close()


def chat_state(req, directory):
    rt = agent_store(directory, Provider(**req["provider"]))
    try:
        return asdict(rt.dynamics)
    finally:
        rt.close()


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


def handle(req, directory, on_delta=None, cancelled=lambda: False):
    action = req.get("action")
    if action == "models":
        return Provider(**req["provider"]).models()
    if action == "run":
        return run(req, directory)
    if action == "chat":
        return chat(req, directory, on_delta, cancelled)
    if action == "state":
        return chat_state(req, directory)
    raise ValueError("unknown action")


def serve(directory, stdin=sys.stdin, stdout=sys.stdout):
    """Long-lived JSON-lines server: concurrent requests by id, streamed chat deltas."""
    lock = threading.Lock()
    cancels = {}

    def send(obj):
        with lock:
            stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
            stdout.flush()

    def work(req):
        rid = req.get("id")
        flag = cancels.setdefault(rid, threading.Event())
        try:
            on_delta = (lambda t: send({"id": rid, "delta": t})) if req.get("stream") else None
            value = handle(req, directory, on_delta, flag.is_set)
            send({"id": rid, "ok": True, "value": value})
        except Exception as exc:
            send({"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            cancels.pop(rid, None)

    send({"ready": True, "pid": os.getpid()})
    for line in stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if not isinstance(req, dict) or "id" not in req:
            continue
        if req.get("action") == "cancel":
            cancels.setdefault(req.get("target"), threading.Event()).set()
            continue
        if req.get("action") == "ping":
            send({"id": req["id"], "ok": True, "value": "pong"})
            continue
        threading.Thread(target=work, args=(req,), daemon=True).start()


def main():
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    if "--serve" in sys.argv[2:]:
        serve(directory)
        return
    try:
        req = json.loads(sys.stdin.readline())
        print(json.dumps({"ok": True, "value": handle(req, directory)}, ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))


if __name__ == "__main__":
    main()
