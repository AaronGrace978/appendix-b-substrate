import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).parent


def run(name):
    return subprocess.run([sys.executable, str(HERE / "examples" / name)], capture_output=True, text=True,
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


if __name__ == "__main__":
    unittest.main()
