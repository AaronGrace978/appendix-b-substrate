import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).parent


def run(name):
    return subprocess.run([sys.executable, str(HERE / "examples" / name)], capture_output=True, text=True,
                          encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                          timeout=120, check=True).stdout


class ExampleTests(unittest.TestCase):
    """The README examples must keep producing the output the README shows."""

    def test_quickstart(self):
        out = run("substrate_quickstart.py")
        self.assertIn("10011 refuted - 10011 takes 210 steps", out)
        self.assertIn("10971 supported - 10971 takes 267 steps", out)
        self.assertIn("verdict: supported ('10971',)", out)

    def test_collatz_verify(self):
        out = run("collatz_verify.py")
        self.assertIn("FIRST_QUALIFYING 10971 STEPS 267 PEAK 975400", out)
        self.assertIn("MAX_BELOW_CANDIDATE_(steps,n): (254, 10617)", out)
        self.assertIn("ALL_TRANSITIONS_VALID: True", out)


class ClaimExampleTests(unittest.TestCase):
    def test_check_claims(self):
        out = run("check_claims.py")
        self.assertIn("✓ 71² − 61 × 71 + 971 = 1681 = 41 × 41", out)
        self.assertIn("✗ 91 is prime — 91 = 7 × 13", out)
        self.assertIn("✓ 10971 takes 267 steps", out)

    def test_check_number_theory(self):
        out = run("check_number_theory.py")
        self.assertIn("✓ prime factorization of 360 is 2^3 × 3^2 × 5 — Python factors 360 = 2³ × 3² × 5", out)
        self.assertIn("✗ 2^100 ≡ 1 (mod 7) — Python computes 2^100 mod 7 = 2", out)
        self.assertIn("✓ The 100th prime is 541", out)


if __name__ == "__main__":
    unittest.main()
