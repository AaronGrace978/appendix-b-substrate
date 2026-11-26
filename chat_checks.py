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
MAX_CHECKS = 16


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
        # Keep the sides Python can evaluate: "f(71) = 71² − 61 × 71 + 971 = 1681" still checks the
        # arithmetic even though "f(71)" is not a number. Dropping a side keeps the chain's claim
        # (every remaining side equal), so this never invents a claim the text did not make.
        kept = []
        for side in sides:
            if not re.fullmatch(_EXPR, side) or not re.search(r"\d", side):
                continue
            try:
                kept.append((side, evaluate(side)))
            except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError):
                continue
        if len(kept) < 2:
            continue
        # A bare "x = 5"-style line has no operator on either side: nothing to check.
        if not any(re.search(r"[+\-*/%²³×·÷^−–]", side.strip().lstrip("-−")) for side, _ in kept):
            continue
        raw = " = ".join(side.strip() for side, _ in kept)
        key = re.sub(r"\s+", "", raw)
        if key in seen:
            continue
        seen.add(key)
        values = [v for _, v in kept]
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


# ---------------------------------------------------------------------------
# Number theory: each claim shape below is matched narrowly, computed exactly,
# and skipped (never refuted) when the numbers are out of the bounded range.
# ---------------------------------------------------------------------------

_NUM = r"\d{1,3}(?:,\d{3})+(?!\d)|\d+"          # 3,628,800 or 3628800
_POW = r"\d{1,30}(?:\s*(?:\^|\*\*)\s*\d{1,12}|[²³])?"   # 7, 2^10, 2**10, 41²
# A claim's subject must stand alone: "1 + 2 + 3 is divisible by 6" is not a claim about 3.
_LEAD = r"(?<![\w.,^#])(?<![+\-*/^×·÷−–=]\s)(?<![+\-*/^×·÷−–=])"
_ORD_WORDS = {w: i for i, w in enumerate(
    "zeroth first second third fourth fifth sixth seventh eighth ninth tenth".split())}
_ORD = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:st|nd|rd|th)|" + "|".join(_ORD_WORDS)
_IS = r"\s*(?:=|is|equals|is equal to)\s*"
_LABELS = ("step|run|round|turn|phase|section|chapter|figure|fig\\.?|table|case|test|trial|example|"
           "version|option|item|line|row|column|page|problem|question|exercise|part|attempt|iteration|"
           "lemma|theorem|claim|rule|level|stage|day|week|player|team|group")
MAX_FACTOR = 10**12          # trial division stays under ~10^6 steps
MAX_SIEVE = 5_000_000        # nth prime up to k ≈ 330,000; prime counts up to 5·10^6
_sieve_cache = bytearray()


def _int(s: str) -> int:
    return int(s.replace(",", ""))


def _nth(k: int) -> str:
    return f"{k}{'th' if 10 <= k % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(k % 10, 'th')}"


def _ordinal(s: str) -> int:
    s = s.lower()
    return _ORD_WORDS[s] if s in _ORD_WORDS else _int(re.sub(r"(st|nd|rd|th)$", "", s))


def _power(s: str, max_digits: int = 4000):
    """Parse '7', '2^10', '2**10' or '41²' as (base, exp); None when the value would be too big."""
    s = s.replace(" ", "").replace("**", "^").replace("²", "^2").replace("³", "^3")
    base, _, exp = s.partition("^")
    b, e = int(base), int(exp or 1)
    if b > 1 and e * math.log10(b) > max_digits:
        return None
    return b, e


def prep_math(text: str) -> str:
    """normalize_math plus the TeX that number-theory claims use."""
    t = normalize_math(text)
    t = re.sub(r"\\pmod\s*\{\s*(\d+)\s*\}|\\pmod\s+(\d+)", lambda m: f"(mod {m.group(1) or m.group(2)})", t)
    for tex, plain in ((r"\times", "×"), (r"\cdot", "·"), (r"\equiv", "≡"), (r"\bmod", " mod "),
                       (r"\mod", " mod "), (r"\nmid", " ∤ "), (r"\mid", " ∣ "), (r"\varphi", "φ"),
                       (r"\phi", "φ"), (r"\sigma", "σ"), (r"\tau", "τ"), (r"\pi", "π")):
        t = t.replace(tex, plain)
    t = re.sub(r"\^\{\s*(\d+)\s*\}", r"^\1", t)
    t = re.sub(r"_\{\s*(\d+)\s*\}", r"_\1", t)
    # "Run 3 has 4 divisors" labels a run; it is not a claim about 3. Mark such numbers as labels.
    t = re.sub(r"\b(" + _LABELS + r")\s+(?=\d)", r"\1 #", t, flags=re.I)
    return re.sub(r"[ \t]{2,}", " ", t)


def factorize(n: int) -> dict[int, int]:
    f, d = {}, 2
    while d * d <= n:
        while n % d == 0:
            f[d] = f.get(d, 0) + 1
            n //= d
        d += 1 if d == 2 else 2
    if n > 1:
        f[n] = f.get(n, 0) + 1
    return f


def _show_factors(f: dict[int, int]) -> str:
    sup = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
    return " × ".join(str(p) + (str(e).translate(sup) if e > 1 else "") for p, e in sorted(f.items())) or "1"


def _sieve(limit: int) -> bytearray:
    global _sieve_cache
    if len(_sieve_cache) <= limit:
        size = max(limit + 1, 1000)
        s = bytearray([1]) * size
        s[0:2] = b"\x00\x00"
        for i in range(2, math.isqrt(size - 1) + 1):
            if s[i]:
                s[i * i::i] = bytes(len(range(i * i, size, i)))
        _sieve_cache = s
    return _sieve_cache


def nth_prime(k: int) -> int | None:
    """The k-th prime (2 is the 1st), or None beyond the sieve bound."""
    if k < 1:
        return None
    limit = 15 if k < 6 else int(k * (math.log(k) + math.log(math.log(k)))) + 1   # Rosser bound
    if limit > MAX_SIEVE:
        return None
    s, seen = _sieve(limit), 0
    for i in range(limit + 1):
        if s[i]:
            seen += 1
            if seen == k:
                return i
    return None


def fibonacci(n: int) -> int:
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def number_functions(text):
    """gcd, lcm, n!, binomial coefficients and Euler's totient."""
    out = []
    names = (r"gcd|hcf|lcm|greatest common (?:divisor|factor)|highest common factor|"
             r"(?:least|lowest) common multiple")
    forms = (r"\b(" + names + r")\s*\(\s*(\d{1,12})\s*,\s*(\d{1,12})\s*\)" + _IS + r"(" + _NUM + r")\b",
             r"\b(" + names + r")\s+of\s+(\d{1,12})\s+and\s+(\d{1,12})" + _IS + r"(" + _NUM + r")\b")
    for pat in forms:
        for m in re.finditer(pat, text, re.I):
            a, b, v = int(m.group(2)), int(m.group(3)), _int(m.group(4))
            is_lcm = m.group(1).lower().startswith("l")
            actual = math.lcm(a, b) if is_lcm else math.gcd(a, b)
            out.append(_check("functions", m.group(0), actual == v,
                              f"Python computes {'lcm' if is_lcm else 'gcd'}({a}, {b}) = {actual}"))
    for m in re.finditer(r"(?<![\w.])(\d{1,4})\s*!(?!=)" + _IS + r"(" + _NUM + r")\b", text):
        n, v = int(m.group(1)), _int(m.group(2))
        if n <= 1000:
            actual = math.factorial(n)
            detail = f"Python computes {n}! = {actual}" if actual < 10**30 else f"{n}! has {len(str(actual))} digits"
            out.append(_check("functions", m.group(0), actual == v, detail))
    binoms = (r"\\[dt]?binom\s*\{\s*(\d{1,5})\s*\}\s*\{\s*(\d{1,5})\s*\}",
              r"\b(?:C|binom|comb|nCr)\s*\(\s*(\d{1,5})\s*,\s*(\d{1,5})\s*\)",
              r"\b(\d{1,5})\s+choose\s+(\d{1,5})")
    for pat in binoms:
        for m in re.finditer(pat + _IS + r"(" + _NUM + r")\b", text):
            n, k, v = int(m.group(1)), int(m.group(2)), _int(m.group(3))
            actual = math.comb(n, k)
            out.append(_check("functions", m.group(0), actual == v, f"Python computes C({n}, {k}) = {actual}"))
    for m in re.finditer(r"φ\s*\(\s*(\d{1,12})\s*\)" + _IS + r"(" + _NUM + r")\b", text):
        n, v = int(m.group(1)), _int(m.group(2))
        if 1 <= n <= MAX_FACTOR:
            actual = math.prod(p ** (e - 1) * (p - 1) for p, e in factorize(n).items())
            out.append(_check("functions", m.group(0), actual == v, f"Python computes φ({n}) = {actual}"))
    return out


def divisibility(text):
    out = []
    forms = ((_LEAD + r"(" + _NUM + r")\s+is\s+(not\s+)?(?:evenly\s+|exactly\s+)?divisible\s+by\s+(" + _NUM + r")\b", 1, 3),
             (_LEAD + r"(" + _NUM + r")\s+is\s+(not\s+)?an?\s+multiple\s+of\s+(" + _NUM + r")\b", 1, 3),
             (_LEAD + r"(\d+)\s+(does\s+not\s+divide|doesn['’]t\s+divide|divides)\s+(" + _NUM + r")\b", 3, 1),
             (_LEAD + r"(\d+)\s*([∣∤])\s*(\d+)\b", 3, 1))
    for pat, ni, di in forms:
        for m in re.finditer(pat, text, re.I):
            n, d = _int(m.group(ni)), _int(m.group(di))
            if d == 0:
                continue
            neg = m.group(2) or ""
            claims = not (neg.lower().startswith(("not", "does", "doesn")) or neg == "∤")
            actual = n % d == 0
            detail = f"{n} = {d} × {n // d}" if actual else f"{n} = {d} × {n // d} + {n % d}"
            out.append(_check("divisibility", m.group(0), claims == actual, detail))
    return out


def modular(text):
    """'a ≡ r (mod m)' congruences and 'a mod m = r' remainders; powers use pow(b, e, m)."""
    out = []

    def residue(lhs, m):
        b, e = _power(lhs, max_digits=10**15)
        return pow(b, e, m)

    for m in re.finditer(_LEAD + r"(" + _POW + r")\s*≡\s*(-?\d+)\s*\(?\s*mod\s*(\d+)\s*\)?", text, re.I):
        lhs, r, mod = m.group(1), int(m.group(2)), int(m.group(3))
        if mod < 1:
            continue
        res = residue(lhs, mod)
        out.append(_check("modular", m.group(0), (res - r) % mod == 0, f"Python computes {lhs} mod {mod} = {res}"))
    for m in re.finditer(_LEAD + r"(" + _POW + r")\s+mod\s+(\d+)" + _IS + r"(-?\d+)\b", text, re.I):
        lhs, mod, r = m.group(1), int(m.group(2)), int(m.group(3))
        if mod < 1:
            continue
        res = residue(lhs, mod)
        out.append(_check("modular", m.group(0), res == r, f"Python computes {lhs} mod {mod} = {res}"))
    return out


def perfect_powers(text):
    out = []
    for m in re.finditer(_LEAD + r"(" + _NUM + r")\s+is\s+(not\s+)?an?\s+perfect\s+(square|cube|power)\b", text, re.I):
        n, neg, kind = _int(m.group(1)), bool(m.group(2)), m.group(3).lower()
        if n > 10**30:
            continue
        if kind == "square":
            r = math.isqrt(n)
            actual, detail = r * r == n, f"{r}² = {r * r}" + ("" if r * r == n else f", {r + 1}² = {(r + 1) ** 2}")
        elif kind == "cube":
            r = round(n ** (1 / 3))
            r = next((c for c in (r - 1, r, r + 1) if c >= 0 and c ** 3 == n), r)
            actual, detail = r ** 3 == n, f"{r}³ = {r ** 3}"
        else:
            hit = None
            for e in range(2, n.bit_length() + 1):
                r = round(n ** (1 / e))
                hit = next(((b, e) for b in (r - 1, r, r + 1) if b > 1 and b ** e == n), None)
                if hit:
                    break
            actual = hit is not None or n in (0, 1)
            detail = f"{n} = {hit[0]}^{hit[1]}" if hit else f"{n} is not b^k for any b ≥ 2, k ≥ 2"
        out.append(_check("powers", m.group(0), actual != neg, detail))
    for m in re.finditer(_LEAD + r"(" + _NUM + r")\s+is\s+(not\s+)?an?\s+power\s+of\s+(\d+)\b", text, re.I):
        n, neg, b = _int(m.group(1)), bool(m.group(2)), int(m.group(3))
        if b < 2 or n < 1 or n > 10**30:
            continue
        e, x = 0, n
        while x % b == 0:
            x //= b
            e += 1
        actual = x == 1
        detail = f"{n} = {b}^{e}" if actual else f"{n} = {b}^{e} × {x}"
        out.append(_check("powers", m.group(0), actual != neg, detail))
    return out


def factorizations(text):
    """'the prime factorization of N is 2³ × 3² × 5': product must equal N and every base must be prime."""
    out = []
    factor = r"\d+(?:\s*(?:\^|\*\*)\s*\d+|[²³⁴⁵⁶⁷⁸⁹]+)?"
    pat = (r"\bprime\s+factori[sz]ation\s+of\s+(" + _NUM + r")\s*(?:is|=|:|equals)\s*(" +
           factor + r"(?:\s*[×*·]\s*" + factor + r")*)")
    sup = str.maketrans("²³⁴⁵⁶⁷⁸⁹", "23456789")
    for m in re.finditer(pat, text, re.I):
        n = _int(m.group(1))
        if not 2 <= n <= MAX_FACTOR:
            continue
        claimed = {}
        for part in re.split(r"\s*[×*·]\s*", m.group(2).strip()):
            part = re.sub(r"(\d+)([²³⁴⁵⁶⁷⁸⁹]+)", lambda x: x.group(1) + "^" + x.group(2).translate(sup), part)
            b, e = _power(part)
            claimed[b] = claimed.get(b, 0) + e
        actual = factorize(n)
        out.append(_check("factorization", m.group(0), claimed == actual, f"Python factors {n} = {_show_factors(actual)}"))
    return out


def divisors(text):
    """Divisor counts, divisor sums, distinct prime factors and perfect numbers."""
    out = []
    words = r"(" + _NUM + "|" + "|".join(WORDS) + r")"

    def num(s):
        return WORDS[s.lower()] if s.lower() in WORDS else _int(s)

    def info(n):
        f = factorize(n)
        tau = math.prod(e + 1 for e in f.values())
        sigma = math.prod((p ** (e + 1) - 1) // (p - 1) for p, e in f.items())
        return f, tau, sigma

    shapes = (
        (_LEAD + r"(" + _NUM + r")\s+has\s+(?:exactly\s+|only\s+)?" + words + r"\s+(?:positive\s+)?(?:divisors|positive\s+factors)\b", "tau", 1, 2),
        (r"\bnumber\s+of\s+(?:positive\s+)?divisors\s+of\s+(" + _NUM + r")" + _IS + words + r"\b", "tau", 1, 2),
        (r"\bτ\s*\(\s*(\d+)\s*\)" + _IS + r"(" + _NUM + r")\b", "tau", 1, 2),
        (_LEAD + r"(" + _NUM + r")\s+has\s+(?:exactly\s+|only\s+)?" + words + r"\s+distinct\s+prime\s+(?:factors|divisors)\b", "omega", 1, 2),
        (r"\bsum\s+of\s+(?:all\s+)?(?:the\s+)?(?:positive\s+)?divisors\s+of\s+(" + _NUM + r")" + _IS + r"(" + _NUM + r")\b", "sigma", 1, 2),
        (r"\bσ\s*\(\s*(\d+)\s*\)" + _IS + r"(" + _NUM + r")\b", "sigma", 1, 2),
    )
    for pat, what, ni, vi in shapes:
        for m in re.finditer(pat, text, re.I):
            n, v = _int(m.group(ni)), num(m.group(vi))
            if not 1 <= n <= MAX_FACTOR:
                continue
            f, tau, sigma = info(n)
            actual = {"tau": tau, "sigma": sigma, "omega": len(f)}[what]
            label = {"tau": "divisors", "sigma": "as its divisor sum", "omega": "distinct prime factors"}[what]
            out.append(_check("divisors", m.group(0), actual == v,
                              f"{n} = {_show_factors(f)} has {actual} {label}"))
    for m in re.finditer(_LEAD + r"(" + _NUM + r")\s+is\s+(not\s+)?a\s+perfect\s+number\b", text, re.I):
        n, neg = _int(m.group(1)), bool(m.group(2))
        if not 1 <= n <= MAX_FACTOR:
            continue
        proper = info(n)[2] - n
        out.append(_check("divisors", m.group(0), (proper == n) != neg, f"proper divisors of {n} sum to {proper}"))
    return out


def digit_claims(text):
    """'N has k digits' and 'the digit sum of N is k'; N may be a power such as 2^100."""
    out = []

    def value(s):
        p = _power(s)
        return None if p is None else p[0] ** p[1]

    subject = r"((?:" + _NUM + r")(?![\d^*²³])|" + _POW + ")"
    shapes = ((_LEAD + subject + r"\s+has\s+(?:exactly\s+|only\s+)?(" + _NUM + r")\s+(?:decimal\s+)?digits\b", "count", 1, 2),
              (r"\b(?:sum\s+of\s+(?:the\s+)?digits|digit\s+sum)\s+of\s+" + subject + _IS + r"(" + _NUM + r")\b", "sum", 1, 2))
    for pat, what, ni, vi in shapes:
        for m in re.finditer(pat, text, re.I):
            n = value(m.group(ni).replace(",", ""))
            if n is None:
                continue
            k, digits = _int(m.group(vi)), str(n)
            actual = len(digits) if what == "count" else sum(map(int, digits))
            detail = f"{m.group(ni)} has {len(digits)} digits" if what == "count" else \
                f"Python sums the digits of {m.group(ni)}: {actual}"
            out.append(_check("digits", m.group(0), actual == k, detail))
    return out


def prime_index(text):
    """'the 100th prime is 541', '541 is the 100th prime', 'there are 25 primes below 100', 'π(100) = 25'."""
    out = []
    for pat, ki, pi in ((r"\bthe\s+(" + _ORD + r")\s+prime(?:\s+number)?" + _IS + r"(" + _NUM + r")\b", 1, 2),
                        (_LEAD + r"(" + _NUM + r")\s+is\s+the\s+(" + _ORD + r")\s+prime\b", 2, 1)):
        for m in re.finditer(pat, text, re.I):
            k, p = _ordinal(m.group(ki)), _int(m.group(pi))
            actual = nth_prime(k)
            if actual is None:
                continue
            out.append(_check("primes", m.group(0), actual == p, f"the {_nth(k)} prime is {actual} (2 is the 1st)"))
    below = r"(less\s+than|below|under|smaller\s+than|up\s+to|not\s+exceeding|at\s+most|≤|<=|<)"
    shapes = ((r"\bthere\s+are\s+(?:exactly\s+)?(" + _NUM + r")\s+primes?(?:\s+numbers)?\s+" + below + r"\s+(" + _NUM + r")\b", 1, 2, 3),
              (r"\bπ\s*\(\s*(" + _NUM + r")\s*\)" + _IS + r"(" + _NUM + r")\b", 2, None, 1))
    for pat, ci, bi, ni in shapes:
        for m in re.finditer(pat, text, re.I):
            count, n = _int(m.group(ci)), _int(m.group(ni))
            inclusive = bi is None or not re.match(r"less|below|under|smaller|<(?!=)", m.group(bi), re.I)
            top = n if inclusive else n - 1
            if top > MAX_SIEVE or top < 0:
                continue
            actual = sum(_sieve(top)[:top + 1])
            out.append(_check("primes", m.group(0), actual == count,
                              f"Python counts {actual} primes {'≤' if inclusive else '<'} {n}"))
    return out


def fibonacci_claims(text):
    """'F(10) = 55' and 'the 10th Fibonacci number is 55', only in text that mentions Fibonacci."""
    if "fibonacci" not in text.lower():
        return []
    out = []
    for m in re.finditer(r"\b(?:F|Fib)\s*(?:\(\s*(\d{1,4})\s*\)|_(\d{1,4}))" + _IS + r"(" + _NUM + r")\b", text):
        n, v = int(m.group(1) or m.group(2)), _int(m.group(3))
        actual = fibonacci(n)
        out.append(_check("fibonacci", m.group(0), actual == v, f"F({n}) = {actual} with F(0) = 0, F(1) = 1"))
    for m in re.finditer(r"\bthe\s+(" + _ORD + r")\s+Fibonacci\s+number" + _IS + r"(" + _NUM + r")\b", text, re.I):
        n, v = _ordinal(m.group(1)), _int(m.group(2))
        if n > 10000:
            continue
        # "nth Fibonacci number" is ambiguous about whether 0 counts as the first, so accept either.
        if v == fibonacci(n):
            ok, detail = True, f"F({n}) = {v} with F(1) = F(2) = 1"
        elif n >= 1 and v == fibonacci(n - 1):
            ok, detail = True, f"F({n - 1}) = {v}, counting 0 as the first Fibonacci number"
        else:
            ok, detail = False, f"F({n}) = {fibonacci(n)} (or {fibonacci(n - 1) if n else 0} counting 0 as the first)"
        out.append(_check("fibonacci", m.group(0), ok, detail))
    return out


NUMBER_THEORY = (number_functions, divisibility, modular, perfect_powers, factorizations,
                 divisors, digit_claims, prime_index, fibonacci_claims)


def extract_checks(text: str) -> list[dict]:
    """Return independently computed checks for recognisable claims in text."""
    if not isinstance(text, str):
        return []
    text = strip_code(text)
    checks = arithmetic(text) + primality(text) + letter_counts(text) + collatz(text)
    nt = prep_math(text)
    for check in NUMBER_THEORY:
        if len(checks) >= MAX_CHECKS:
            break
        checks += check(nt)
    return checks[:MAX_CHECKS]
