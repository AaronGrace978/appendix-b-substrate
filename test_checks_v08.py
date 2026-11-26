import time
import unittest

from chat_checks import extract_checks, factorize, fibonacci, nth_prime


def verdicts(text, kind=None):
    return [(c["claim"], c["verdict"]) for c in extract_checks(text) if kind is None or c["kind"] == kind]


def ok(text, kind):
    got = verdicts(text, kind)
    return bool(got) and all(v == "supported" for _, v in got)


def bad(text, kind):
    got = verdicts(text, kind)
    return bool(got) and all(v == "refuted" for _, v in got)


class NumberFunctionTests(unittest.TestCase):
    def test_gcd_lcm(self):
        self.assertTrue(ok("gcd(12, 18) = 6 and lcm(4, 6) = 12.", "functions"))
        self.assertTrue(ok("The greatest common divisor of 84 and 36 is 12.", "functions"))
        self.assertTrue(ok("The least common multiple of 4 and 6 is 12.", "functions"))
        self.assertTrue(bad("lcm(4,6) = 24", "functions"))
        self.assertTrue(ok("gcd(120,180) = 60", "functions"))  # arguments are never read as 120,180

    def test_factorial_binomial_totient(self):
        self.assertTrue(ok("10! = 3,628,800 and 5! = 120", "functions"))
        self.assertTrue(bad("5! = 121", "functions"))
        self.assertEqual(verdicts("5 != 6 and 5!=6", "functions"), [])
        self.assertTrue(ok(r"C(10, 3) = 120, \binom{52}{5} = 2598960, 6 choose 2 is 15", "functions"))
        self.assertTrue(ok(r"φ(36) = 12 and \varphi(7) = 6", "functions"))
        self.assertTrue(bad("φ(10) = 5", "functions"))


class DivisibilityAndModularTests(unittest.TestCase):
    def test_divisibility(self):
        self.assertTrue(ok("1681 is divisible by 41. 1681 is not divisible by 7. 7 divides 91. 3 \\mid 12.", "divisibility"))
        self.assertTrue(bad("100 is a multiple of 8", "divisibility"))
        self.assertTrue(bad("7 does not divide 91", "divisibility"))
        self.assertTrue(ok("1,001 is divisible by 13", "divisibility"))

    def test_subject_must_stand_alone(self):
        self.assertEqual(verdicts("1 + 2 + 3 is divisible by 4. x = 5 is divisible by 2."), [])
        self.assertEqual(verdicts("Run 3 has 4 divisors. Step 12 is divisible by 5. Section 3 is the 2nd prime."), [])
        self.assertEqual(verdicts("Markdown | 12 | 5 | table"), [])

    def test_congruences(self):
        self.assertTrue(ok("2^10 ≡ 1 (mod 11), 17 mod 5 = 2, 2^10 mod 1000 = 24", "modular"))
        self.assertTrue(bad(r"2^{100} \equiv 1 \pmod{7}", "modular"))
        self.assertTrue(ok("2^1000000 ≡ 1 (mod 3)", "modular"))   # computed with pow(b, e, m), not 2**1000000
        self.assertTrue(ok("100 ≡ -5 (mod 7)", "modular"))
        self.assertTrue(bad("100 ≡ 3 (mod 7)", "modular"))


class StructureTests(unittest.TestCase):
    def test_perfect_powers(self):
        self.assertTrue(ok("1681 is a perfect square. 1000 is a perfect cube. 1024 is a power of 2.", "powers"))
        self.assertTrue(ok("50 is not a perfect square. 1000 is a perfect power.", "powers"))
        self.assertTrue(bad("50 is a perfect square", "powers"))
        self.assertTrue(bad("96 is a power of 2", "powers"))

    def test_factorization(self):
        self.assertTrue(ok("The prime factorization of 360 is 2³ × 3² × 5.", "factorization"))
        self.assertTrue(ok(r"The prime factorization of 1001 is 7 \times 11 \times 13", "factorization"))
        self.assertTrue(bad("The prime factorization of 84 is 2^2 * 3 * 7 * 2", "factorization"))
        self.assertTrue(bad("The prime factorization of 36 is 4 × 9", "factorization"))  # right product, not primes

    def test_divisors(self):
        self.assertTrue(ok("12 has 6 divisors. The number of divisors of 360 is 24. σ(12) = 28. τ(36) = 9.", "divisors"))
        self.assertTrue(ok("30 has 3 distinct prime factors. 28 is a perfect number. 12 is not a perfect number.", "divisors"))
        self.assertTrue(bad("12 has eight divisors", "divisors"))
        self.assertEqual(verdicts("1681 has two factors", "divisors"), [])  # "factors" alone is too ambiguous

    def test_digits(self):
        self.assertTrue(ok("2^100 has 31 digits. 3,628,800 has 7 digits. The sum of the digits of 2^15 is 26.", "digits"))
        self.assertTrue(bad("2^100 has 30 digits", "digits"))
        self.assertEqual(verdicts("2^99999999 has 5 digits", "digits"), [])  # too large: skipped, not refuted


class SequenceTests(unittest.TestCase):
    def test_nth_prime_and_counts(self):
        self.assertEqual([nth_prime(k) for k in range(1, 11)], [2, 3, 5, 7, 11, 13, 17, 19, 23, 29])
        self.assertTrue(ok("The 100th prime is 541. 7919 is the 1000th prime. The first prime is 2.", "primes"))
        self.assertTrue(ok("There are 25 primes below 100. There are 4 primes up to 10. π(1000) = 168.", "primes"))
        self.assertTrue(bad("The 10,001st prime is 104729", "primes"))
        self.assertEqual(verdicts("There are 25 primes up to 97 and π(10) = 5", "primes"),
                         [("There are 25 primes up to 97", "supported"), ("π(10) = 5", "refuted")])

    def test_prime_count_boundaries(self):
        self.assertTrue(ok("There are 4 primes less than 11. There are 5 primes up to 11.", "primes"))
        self.assertTrue(bad("There are 5 primes below 11", "primes"))

    def test_fibonacci(self):
        self.assertEqual([fibonacci(n) for n in range(8)], [0, 1, 1, 2, 3, 5, 8, 13])
        self.assertTrue(ok("Fibonacci: F(10) = 55, F_{12} = 144, the 10th Fibonacci number is 55", "fibonacci"))
        self.assertTrue(ok("The 10th Fibonacci number is 34 if the sequence starts at 0.", "fibonacci"))
        self.assertTrue(bad("Fibonacci: F(20) = 6764. The 7th Fibonacci number is 20.", "fibonacci"))
        self.assertEqual(verdicts("F(10) = 54 is a force in newtons."), [])  # no Fibonacci context

    def test_factorize(self):
        self.assertEqual(factorize(360), {2: 3, 3: 2, 5: 1})
        self.assertEqual(factorize(999999999989), {999999999989: 1})


class BoundsTests(unittest.TestCase):
    def test_worst_cases_stay_fast(self):
        start = time.monotonic()
        extract_checks("The 300000th prime is 4256233. π(5000000) = 348513. "
                       + " ".join(f"{999999999989} is a perfect number." for _ in range(30)))
        self.assertLess(time.monotonic() - start, 10)

    def test_out_of_range_is_skipped(self):
        self.assertEqual(verdicts("The 9000000th prime is 7. π(10000000000) = 3. 2000! = 5"), [])

    def test_old_checks_unchanged(self):
        got = verdicts("91 is prime. 7 * 8 = 56. There are 3 r's in strawberry.")
        self.assertEqual(got, [("7 * 8 = 56", "supported"), ("91 is prime", "refuted"),
                               ("3 r's in strawberry", "supported")])


if __name__ == "__main__":
    unittest.main()
