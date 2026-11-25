"""Deterministic checks for claims found in free-text model replies.

Zero dependencies. Each check is computed independently in Python; the model's
text is only parsed for candidate claims, never trusted for the result.
Unrecognised claims are simply not checked: absence of a check is not support.
"""
from __future__ import annotations

import ast
import math
import operator
import re

SUPERSCRIPTS = str.maketrans({"²": "**2", "³": "**3", "×": "*", "·": "*", "÷": "/", "^": "**", "−": "-", "–": "-"})
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_EXPR = r"[\d\s+\-*/%().²³×·÷^−–]+"
WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve".split())}
MAX_CHECKS = 12


def evaluate(expr: str):
    """Safely evaluate a small arithmetic expression; raise ValueError otherwise."""
    expr = expr.translate(SUPERSCRIPTS).strip()
    if not expr or len(expr) > 200 or not re.search(r"\d", expr):
        raise ValueError("not arithmetic")
    tree = ast.parse(expr, mode="eval")

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = walk(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 64 or abs(left) > 10**6):
                raise ValueError("exponent too large")
            if abs(left) > 10**30 or abs(right) > 10**30:
                raise ValueError("operand too large")
            return _OPS[type(node.op)](left, right)
        raise ValueError("unsupported expression")
    return walk(tree)


def _same(a, b):
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def is_prime(n: int) -> bool:
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    return all(n % d for d in range(3, math.isqrt(n) + 1, 2))


def _check(kind, claim, ok, detail):
    return {"kind": kind, "claim": claim.strip().strip("\"'“”‘’`.,;:"), "verdict": "supported" if ok else "refuted", "detail": detail}


def arithmetic(text):
    out = []
    candidates = re.findall(r"`([^`\n]{3,200})`", text)
    candidates += [m.group(0) for m in re.finditer(r"(?<![\w.])\d[\d\s+\-*/%().²³×·÷^−–]*=\s*" + _EXPR + r"(?:=\s*" + _EXPR + r")*", text)]
    seen = set()
    for raw in candidates:
        if "=" not in raw or any(op in raw for op in ("==", "!=", "<=", ">=")):
            continue
        sides = [s.strip(" .,;:") for s in raw.split("=")]
        if len(sides) < 2 or any(not re.fullmatch(_EXPR, s) or not re.search(r"\d", s) for s in sides):
            continue
        # A bare "x = 5"-style line has no operator on either side: nothing to check.
        if not any(re.search(r"[+\-*/%²³×·÷^]", s.strip().lstrip("-")) for s in sides):
            continue
        key = re.sub(r"\s+", "", raw)
        if key in seen:
            continue
        seen.add(key)
        try:
            values = [evaluate(s) for s in sides]
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
            continue
        ok = all(_same(values[0], v) for v in values[1:])
        shown = " = ".join(f"{v:g}" if isinstance(v, float) else str(v) for v in values)
        out.append(_check("arithmetic", raw, ok, "Python evaluates: " + shown))
    return out


def primality(text):
    out = []
    for m in re.finditer(r"\b(\d{1,12})\s+is\s+(not\s+)?(?:an?\s+)?(prime|composite)\b", text, re.I):
        n, negated, word = int(m.group(1)), bool(m.group(2)), m.group(3).lower()
        claims_prime = (word == "prime") != negated
        if n < 2 and not claims_prime and word == "composite":
            continue
        actual = is_prime(n)
        if not actual and n >= 4:
            d = next(d for d in range(2, math.isqrt(n) + 1) if n % d == 0)
            detail = f"{n} = {d} × {n // d}"
        else:
            detail = f"{n} is {'prime' if actual else 'not prime'}"
        out.append(_check("primality", m.group(0), claims_prime == actual, detail))
    return out


def letter_counts(text):
    out = []
    pat = (r"\b(\d{1,3}|" + "|".join(WORDS) + r")\s+(?:occurrences?\s+of\s+(?:the\s+letter\s+)?)?"
           r"[\"'“‘]?([A-Za-z])[\"'”’]?(?:['’]s|s)?\s+in\s+(?:the\s+word\s+)?[\"'“‘]?([A-Za-z]{2,40})\b")
    for m in re.finditer(pat, text, re.I):
        raw_n = m.group(1).lower()
        n = int(raw_n) if raw_n.isdigit() else WORDS[raw_n]
        letter, word = m.group(2), m.group(3)
        if word.lower() in ("the", "a", "an", "word", "total", "it", "this"):
            continue
        actual = word.casefold().count(letter.casefold())
        positions = [i for i, c in enumerate(word.casefold(), 1) if c == letter.casefold()]
        out.append(_check("letters", m.group(0), n == actual,
                          f"'{word}' contains {actual} × '{letter}' at positions {positions}"))
    return out


def collatz_steps(n: int, limit: int = 100000) -> int | None:
    steps = 0
    while n != 1 and steps < limit:
        n = n // 2 if n % 2 == 0 else 3 * n + 1
        steps += 1
    return steps if n == 1 else None


def normalize_math(text: str) -> str:
    """Undo common LaTeX so claims written in TeX can still be checked."""
    t = text.replace("{,}", "").replace("\\,", "").replace("\\!", "")
    t = re.sub(r"\\(?:text|mathrm|mathbf|boxed|operatorname)\s*\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"\\[()\[\]]|\\q?quad|\$\$?", " ", t)
    return t


def collatz(text):
    """Check 'n takes k steps' claims when the text is about Collatz sequences."""
    if "collatz" not in text.lower() and "3n+1" not in text.replace(" ", "").lower():
        return []
    out, seen = [], set()
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", normalize_math(text))
    pat = (r"\b(?:n\s*=\s*)?(\d{1,9})\b[^.\n\d]{0,45}?\b(?:takes|requires|needs|has|reaches 1 in|with|"
           r"stopping time (?:is|of)|is exactly)\s+(?:exactly\s+|only\s+)?(\d{1,5})\s+(?:total\s+)?steps")
    for m in re.finditer(pat, t, re.I):
        n, k = int(m.group(1)), int(m.group(2))
        if n < 1 or (n, k) in seen:
            continue
        seen.add((n, k))
        actual = collatz_steps(n)
        if actual is None:
            continue
        out.append(_check("collatz", f"{n} takes {k} steps", actual == k, f"Python counts {actual} steps from {n} to 1"))
    return out


def strip_code(text: str) -> str:
    """Remove fenced code blocks: code is evidence, not a claim."""
    return re.sub(r"```.*?(?:```|$)", " ", text or "", flags=re.S)


def extract_checks(text: str) -> list[dict]:
    """Return independently computed checks for recognisable claims in text."""
    if not isinstance(text, str):
        return []
    text = strip_code(text)
    checks = arithmetic(text) + primality(text) + letter_counts(text) + collatz(text)
    return checks[:MAX_CHECKS]
