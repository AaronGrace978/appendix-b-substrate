"""Number-theory claim checks from the README, in plain text and LaTeX."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chat_checks import extract_checks

reply = r"""The prime factorization of 360 is 2^3 \times 3^2 \times 5, so 360 has 24 divisors.
Also gcd(84, 36) = 12, \binom{10}{3} = 120, 10! = 3,628,800 and 2^{100} \equiv 1 \pmod{7}.
The 100th prime is 541, there are 25 primes below 100, and 1001 is divisible by 13."""

for c in extract_checks(reply):
    print("✓" if c["verdict"] == "supported" else "✗", c["claim"], "—", c["detail"])
