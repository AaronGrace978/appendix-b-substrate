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
from code_runner import find_runs, result_message, run_code
from runtime_v2 import Evidence, Verdict, claim_id

PROVIDERS = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "ollama-cloud": ("https://ollama.com/api", "OLLAMA_API_KEY"),
    "ollama-local": ("http://127.0.0.1:11434/api", None),
}


MAX_OUT = 32768      # reasoning models spend output tokens thinking before answering
ANTHROPIC_OUT = 16000


EMPTY_HINT = ("the model used its whole output budget reasoning and returned no final answer; "
              "try again, ask a narrower question, or pick a non-reasoning model")


RETRY_CODES = (429, 500, 502, 503, 504, 529)
RETRY_DELAYS = (1.5, 4.0)

TOOL_SPEC = {
    "name": "run_python",
    "description": ("Run Python 3 code (standard library only) and get its stdout and stderr back. Use it for any "
                    "calculation, search or exhaustive check instead of working it out by hand. Print the values you "
                    "need. 15 second limit, no network, no access to the user's files."),
    "parameters": {"type": "object", "properties": {"code": {"type": "string", "description": "Python source to run"}},
                   "required": ["code"]},
}


class ProviderError(RuntimeError):
    def __init__(self, name, code, detail=""):
        self.code, self.detail = code, detail
        super().__init__(f"{name}: HTTP {code}" + (f" - {detail[:300]}" if detail else ""))


class Provider:
    def __init__(self, name, key="", model=""):
        self.name = name
        self.base, env = PROVIDERS[name]
        self.key = key or (os.getenv(env, "") if env else "")
        self.model = model
        self.calls = 0
        self.usage = []

    def request(self, path, body=None):
        req = urllib.request.Request(self.base + path, None if body is None else
                                     json.dumps(body).encode(), self._headers())
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                with urllib.request.urlopen(req, timeout=120) as res:
                    return json.loads(res.read(8 * 1024 * 1024))
            except urllib.error.HTTPError as exc:
                if exc.code in RETRY_CODES and attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
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
            body = {"model": self.model, "input": messages, "max_output_tokens": MAX_OUT, "store": False}
            if system:
                body["instructions"] = system
            data = self.request("/responses", body)
            out = "".join(c["text"] for m in data.get("output", []) if m.get("type") == "message"
                          for c in m.get("content", []) if c.get("type") == "output_text")
        elif self.name == "anthropic":
            body = {"model": self.model, "max_tokens": ANTHROPIC_OUT, "messages": messages}
            if system:
                body["system"] = system
            data = self.request("/messages", body)
            out = "".join(c["text"] for c in data["content"] if c.get("type") == "text")
        else:
            full = ([{"role": "system", "content": system}] if system else []) + messages
            if self.name.startswith("ollama"):
                data = self.request("/chat", {"model": self.model, "stream": False, "messages": full,
                                              "options": {"num_predict": MAX_OUT}})
                out = data["message"].get("content") or ""
                self.last_thinking = data["message"].get("thinking") or ""
            else:
                data = self.request("/chat/completions", {"model": self.model, "max_tokens": MAX_OUT,
                                                          "messages": full})
                msg = data["choices"][0]["message"]
                out = msg.get("content") or ""
                self.last_thinking = msg.get("reasoning") or msg.get("reasoning_content") or ""
        self.usage.append(data.get("usage") or {k: data[k] for k in ("prompt_eval_count", "eval_count") if k in data})
        if not isinstance(out, str) or not out.strip():
            raise ValueError(EMPTY_HINT if getattr(self, "last_thinking", "") else "empty model output")
        return out

    def _headers(self, stream=False):
        headers = {"Content-Type": "application/json"}
        if stream:
            headers["Accept"] = "text/event-stream"
        if self.name == "anthropic":
            headers.update({"x-api-key": self.key, "anthropic-version": "2023-06-01"})
        elif self.key and self.name != "ollama-local":
            headers["Authorization"] = "Bearer " + self.key
        if self.name not in ("ollama-local", "openrouter") and not self.key:
            raise ValueError("API key required")
        return headers

    def _stream_lines(self, path, body, cancelled):
        """Yield decoded text lines from a streaming HTTP response.

        Transient failures (HTTP 429/5xx, dropped connections) are retried before
        any data arrives; nothing is retried once the reply has started.
        """
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(), self._headers(True))
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                res = urllib.request.urlopen(req, timeout=300)
                break
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    raw = exc.read(65536)
                    err = json.loads(raw)
                    e = err.get("error")
                    detail = e.get("message", "") if isinstance(e, dict) else str(e or err.get("message") or "")
                except Exception:
                    pass
                if exc.code in RETRY_CODES and attempt < len(RETRY_DELAYS) and not cancelled():
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                raise ProviderError(self.name, exc.code, detail) from None
            except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
                if attempt < len(RETRY_DELAYS) and not cancelled():
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                raise RuntimeError(f"{self.name}: connection failed ({getattr(exc, 'reason', exc)})") from None
        with res:
            for raw in res:
                if cancelled():
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line:
                    yield line

    def stream(self, messages, system="", on_delta=lambda s: None, cancelled=lambda: False,
               on_thinking=lambda s: None, tools=False):
        """Stream a multi-turn reply, calling on_delta with each text fragment.

        With tools=True the run_python tool is offered natively; requested calls are
        left in self.last_tool_calls as [{"id", "name", "arguments"}].
        """
        if not self.model:
            raise ValueError("select a model")
        self.calls += 1
        parts, thoughts, usage = [], [], {}
        self.stop_reason = ""
        self.last_tool_calls = []
        self.last_output_items = []
        pending = {}  # index -> partial tool call

        def think(text):
            if text:
                thoughts.append(text)
                on_thinking(text)

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

        def args_of(raw):
            if isinstance(raw, dict):
                return raw
            try:
                value = json.loads(raw or "{}")
                return value if isinstance(value, dict) else {"code": str(value)}
            except ValueError:
                return {"code": raw or ""}

        if self.name == "openai":
            body = {"model": self.model, "input": messages, "max_output_tokens": MAX_OUT, "store": False, "stream": True}
            if system:
                body["instructions"] = system
            if tools:
                body["tools"] = [{"type": "function", **TOOL_SPEC}]
                body["include"] = ["reasoning.encrypted_content"]
            for ev in sse(self._stream_lines("/responses", body, cancelled)):
                t = ev.get("type")
                if t == "response.output_text.delta":
                    emit(ev.get("delta", ""))
                elif t == "response.reasoning_summary_text.delta":
                    think(ev.get("delta", ""))
                elif t == "response.output_item.done":
                    item = ev.get("item") or {}
                    self.last_output_items.append(item)
                    if item.get("type") == "function_call":
                        self.last_tool_calls.append({"id": item.get("call_id"), "name": item.get("name"),
                                                     "arguments": args_of(item.get("arguments"))})
                elif t == "response.completed":
                    usage = (ev.get("response") or {}).get("usage") or {}
                elif t == "response.incomplete":
                    self.stop_reason = "length"
                elif t in ("error", "response.failed"):
                    raise RuntimeError("openai: " + json.dumps(ev.get("error") or ev.get("response", {}).get("error"))[:300])
        elif self.name == "anthropic":
            body = {"model": self.model, "max_tokens": ANTHROPIC_OUT, "messages": messages, "stream": True}
            if system:
                body["system"] = system
            if tools:
                body["tools"] = [{"name": TOOL_SPEC["name"], "description": TOOL_SPEC["description"],
                                  "input_schema": TOOL_SPEC["parameters"]}]
            for ev in sse(self._stream_lines("/messages", body, cancelled)):
                t = ev.get("type")
                if t == "content_block_start" and (ev.get("content_block") or {}).get("type") == "tool_use":
                    b = ev["content_block"]
                    pending[ev.get("index", 0)] = {"id": b.get("id"), "name": b.get("name"), "json": ""}
                elif t == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                    emit(ev["delta"]["text"])
                elif t == "content_block_delta" and ev["delta"].get("type") == "thinking_delta":
                    think(ev["delta"].get("thinking", ""))
                elif t == "content_block_delta" and ev["delta"].get("type") == "input_json_delta":
                    pending.setdefault(ev.get("index", 0), {"id": None, "name": None, "json": ""})["json"] += \
                        ev["delta"].get("partial_json", "")
                elif t == "message_start":
                    usage.update((ev.get("message") or {}).get("usage") or {})
                elif t == "message_delta":
                    usage.update(ev.get("usage") or {})
                    self.stop_reason = (ev.get("delta") or {}).get("stop_reason") or ""
                elif t == "error":
                    raise RuntimeError("anthropic: " + str((ev.get("error") or {}).get("message", ""))[:300])
            for i in sorted(pending):
                c = pending[i]
                self.last_tool_calls.append({"id": c["id"], "name": c["name"], "arguments": args_of(c["json"])})
        elif self.name.startswith("ollama"):
            full = ([{"role": "system", "content": system}] if system else []) + messages
            body = {"model": self.model, "stream": True, "messages": full, "options": {"num_predict": MAX_OUT}}
            if tools:
                body["tools"] = [{"type": "function", "function": TOOL_SPEC}]
            for line in self._stream_lines("/chat", body, cancelled):
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("error"):
                    raise RuntimeError(f"{self.name}: {str(ev['error'])[:300]}")
                msg = ev.get("message") or {}
                think(msg.get("thinking") or "")
                emit(msg.get("content") or "")
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    self.last_tool_calls.append({"id": tc.get("id"), "name": fn.get("name"),
                                                 "arguments": args_of(fn.get("arguments"))})
                if ev.get("done"):
                    usage = {k: ev[k] for k in ("prompt_eval_count", "eval_count") if k in ev}
                    self.stop_reason = ev.get("done_reason") or ""
        else:
            full = ([{"role": "system", "content": system}] if system else []) + messages
            body = {"model": self.model, "max_tokens": MAX_OUT, "messages": full, "stream": True}
            if tools:
                body["tools"] = [{"type": "function", "function": TOOL_SPEC}]
            for ev in sse(self._stream_lines("/chat/completions", body, cancelled)):
                if ev.get("error"):
                    raise RuntimeError("openrouter: " + str(ev["error"])[:300])
                for choice in ev.get("choices") or []:
                    if choice.get("finish_reason"):
                        self.stop_reason = choice["finish_reason"]
                    d = choice.get("delta") or {}
                    think(d.get("reasoning") or d.get("reasoning_content") or "")
                    emit(d.get("content") or "")
                    for tc in d.get("tool_calls") or []:
                        c = pending.setdefault(tc.get("index", 0), {"id": None, "name": None, "json": ""})
                        c["id"] = tc.get("id") or c["id"]
                        fn = tc.get("function") or {}
                        c["name"] = fn.get("name") or c["name"]
                        c["json"] += fn.get("arguments") or ""
                if ev.get("usage"):
                    usage = ev["usage"]
            for i in sorted(pending):
                c = pending[i]
                self.last_tool_calls.append({"id": c["id"], "name": c["name"], "arguments": args_of(c["json"])})
        for i, c in enumerate(self.last_tool_calls):
            c["id"] = c.get("id") or f"call_{self.calls}_{i}"
        self.usage.append(usage)
        text = "".join(parts)
        self.last_thinking = "".join(thoughts)
        if not text.strip() and not cancelled() and not self.last_thinking.strip() and not self.last_tool_calls:
            raise ValueError("empty model output")
        return text

    def tool_followup(self, text, calls, outputs):
        """Messages to append after a native tool round, in this provider's format."""
        if self.name == "openai":
            items = [i for i in self.last_output_items if i.get("type") in ("reasoning", "message", "function_call")]
            return items + [{"type": "function_call_output", "call_id": c["id"], "output": o}
                            for c, o in zip(calls, outputs)]
        if self.name == "anthropic":
            content = ([{"type": "text", "text": text}] if text.strip() else []) + \
                      [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]} for c in calls]
            return [{"role": "assistant", "content": content},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": c["id"], "content": o}
                                                 for c, o in zip(calls, outputs)]}]
        if self.name.startswith("ollama"):
            msg = {"role": "assistant", "content": text,
                   "tool_calls": [{"type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                                  for c in calls]}
            if getattr(self, "last_thinking", ""):
                msg["thinking"] = self.last_thinking
            return [msg] + [{"role": "tool", "tool_name": c["name"], "content": o} for c, o in zip(calls, outputs)]
        return [{"role": "assistant", "content": text or None,
                 "tool_calls": [{"id": c["id"], "type": "function",
                                 "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                for c in calls]}] + \
               [{"role": "tool", "tool_call_id": c["id"], "content": o} for c, o in zip(calls, outputs)]

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


CODE_PROMPT = (
    "You can run Python 3 code (standard library only). To run code, write a fenced block whose first line is "
    "```python run and then stop writing. The app runs it and replies with its stdout and stderr, then you continue. "
    "Use code for any calculation, search or exhaustive check instead of working it out by hand, and print the "
    "values you need. Each run has a 15 second limit, no network and no access to the user's files. "
    "Give your best answer early, then confirm it with code. At most {rounds} rounds of runs per reply; "
    "ordinary ```python blocks without 'run' are shown, not executed."
)
TOOL_PROMPT = (
    "You have a run_python tool that executes Python 3 (standard library only) and returns stdout and stderr. "
    "Call it for any calculation, search or exhaustive check instead of working it out by hand, and print the "
    "values you need. Do not just say you will compute something: call the tool in this reply. "
    "Give your best answer early, then confirm it with the tool. At most {rounds} rounds of tool calls per reply."
)
CODE_ROUNDS = 4
CODE_RUNS_PER_ROUND = 3


def chat(req, directory=None, on_delta=None, cancelled=lambda: False, on_thinking=lambda s: None,
         on_tool=lambda r: None):
    messages = req.get("messages")
    if type(messages) is not list or not 1 <= len(messages) <= 400:
        raise ValueError("messages must be a list of 1-400 turns")
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
    tools = bool(req.get("code"))
    native = tools and req.get("native_tools", True)
    base_system = system
    def system_for(native_):
        if not tools:
            return base_system
        prompt = (TOOL_PROMPT if native_ else CODE_PROMPT).format(rounds=CODE_ROUNDS)
        return (prompt + "\n\n" + base_system).strip()
    p = Provider(**req["provider"])
    start = time.monotonic()
    convo, turns, segments, texts, thoughts = list(clean), [], [], [], []
    fallback_note = ""
    for round_ in range(CODE_ROUNDS + 1):
        try:
            if on_delta is None and not native:
                text = p.complete(convo, system_for(False))
                p.last_tool_calls = []
            else:
                text = p.stream(convo, system_for(native), on_delta or (lambda t: None), cancelled, on_thinking,
                                tools=native)
        except ProviderError as exc:
            if native and exc.code in (400, 404, 422) and round_ == 0:
                # Model or endpoint does not accept tools: fall back to fenced ```python run blocks.
                native = False
                fallback_note = "This model doesn't support tool calls; using code blocks instead."
                text = p.stream(convo, system_for(False), on_delta or (lambda t: None), cancelled, on_thinking)
                p.last_tool_calls = []
            else:
                raise
        thoughts.append(getattr(p, "last_thinking", "") or "")
        calls = [c for c in (getattr(p, "last_tool_calls", None) or []) if c.get("name") == TOOL_SPEC["name"]]
        if not text.strip() and not calls:
            break
        if text.strip():
            texts.append(text)
            segments.append({"type": "text", "text": text})
        fenced = find_runs(text) if tools and not calls and not cancelled() else []
        if not calls and not fenced:
            turns.append({"role": "assistant", "content": text})
            break
        if round_ == CODE_ROUNDS:
            segments.append({"type": "notice", "text": f"Stopped after {CODE_ROUNDS} rounds of code runs."})
            turns.append({"role": "assistant", "content": text or "(stopped before answering)"})
            break
        codes = [str((c.get("arguments") or {}).get("code") or "") for c in calls] if calls else fenced
        results = []
        for code in codes[:CODE_RUNS_PER_ROUND]:
            if cancelled():
                break
            r = run_code(code) if code.strip() else {
                "code": code, "stdout": "", "stderr": "run_python was called without code.", "exit": 1,
                "timed_out": False, "ok": False, "seconds": 0}
            results.append(r)
            segments.append({"type": "run", **r})
            on_tool(r)
        if not results:
            turns.append({"role": "assistant", "content": text or "(stopped)"})
            break
        feedback = result_message(results)
        if calls:
            outputs = [result_message([r]) for r in results]
            calls = calls[:len(results)]
            convo += p.tool_followup(text, calls, outputs)
        else:
            convo += [{"role": "assistant", "content": text}, {"role": "user", "content": feedback}]
        shown = (text.strip() + "\n\n" if text.strip() else "") + "\n\n".join(
            f"[ran code]\n```python\n{r['code'].rstrip()}\n```" for r in results)
        turns += [{"role": "assistant", "content": shown}, {"role": "user", "content": feedback}]
    if fallback_note:
        segments.insert(0, {"type": "notice", "text": fallback_note})
    thinking = "\n\n".join(t for t in thoughts if t)
    full = "\n\n".join(texts)
    base = {"usage": p.usage[-1] if p.usage else {}, "seconds": time.monotonic() - start,
            "provider": p.name, "model": p.model, "thinking": thinking, "segments": segments,
            "runs": sum(1 for s in segments if s["type"] == "run")}
    if not full.strip() and not cancelled():
        # Reasoning arrived but no answer: report it rather than failing silently.
        return {**base, "text": "", "notice": EMPTY_HINT, "checks": [], "state": None, "cancelled": False, "turns": []}
    if turns and turns[-1]["role"] == "user":
        # Answer never arrived after the last run (e.g. reasoning only): keep the history valid.
        turns.append({"role": "assistant", "content": "(no answer after the code results)"})
        segments.append({"type": "notice", "text": "The model returned no answer after the code results."})
    checks = extract_checks(full) if req.get("verify", True) else []
    state = None
    if directory is not None and checks:
        state = observe_checks(directory, p, checks)
    return {**base, "text": full, "turns": turns, "checks": checks, "state": state,
            "truncated": getattr(p, "stop_reason", "") in ("length", "max_tokens", "max_output_tokens"),
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


def handle(req, directory, on_delta=None, cancelled=lambda: False, on_thinking=lambda s: None,
           on_tool=lambda r: None):
    action = req.get("action")
    if action == "models":
        return Provider(**req["provider"]).models()
    if action == "run":
        return run(req, directory)
    if action == "chat":
        return chat(req, directory, on_delta, cancelled, on_thinking, on_tool)
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
            on_thinking = (lambda t: send({"id": rid, "thinking": t})) if req.get("stream") else (lambda t: None)
            on_tool = (lambda r: send({"id": rid, "tool": r})) if req.get("stream") else (lambda r: None)
            value = handle(req, directory, on_delta, flag.is_set, on_thinking, on_tool)
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
