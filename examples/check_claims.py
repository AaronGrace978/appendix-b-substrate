"""Independent claim checks from the README: Python recomputes what a reply asserts."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chat_checks import extract_checks

reply = """In the Collatz sequence 10,971 takes 267 steps. Also 91 is prime and 7 * 8 = 56.
The run breaks at f(71) = 71² − 61 × 71 + 971 = 1681 = 41 × 41."""

for c in extract_checks(reply):
    print("✓" if c["verdict"] == "supported" else "✗", c["claim"], "—", c["detail"])
