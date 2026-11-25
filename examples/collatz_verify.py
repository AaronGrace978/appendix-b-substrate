"""Exhaustive Collatz check from the README: smallest n > 10,000 with at least 260 steps."""

def trajectory(n):
    path = [n]
    while n != 1:
        n = n // 2 if n % 2 == 0 else 3 * n + 1
        path.append(n)
    return path

best = 0
for n in range(10001, 10**6):
    path = trajectory(n)
    steps = len(path) - 1
    if steps > best:
        best = steps
        print("new maximum in searched range:", n, steps, "peak", max(path))
    if steps >= 260:
        break
print("FIRST_QUALIFYING", n, "STEPS", steps, "PEAK", max(path))

# Nothing between 10,001 and the candidate may qualify.
print("MAX_BELOW_CANDIDATE_(steps,n):",
      max((len(trajectory(m)) - 1, m) for m in range(10001, n)))

# Independent recheck: rebuild the path and validate every transition.
t = trajectory(10971)
print("CANDIDATE_RECHECK:", (len(t) - 1, max(t)))
print("TRAJECTORY_LENGTH_INCLUDING_BOTH_ENDPOINTS:", len(t))
print("EDGE_COUNT:", len(t) - 1)
print("FIRST_12_TERMS:", t[:12])
print("LAST_12_TERMS:", t[-12:])
print("MAX_TRAJECTORY_VALUE:", max(t))
print("ALL_TRANSITIONS_VALID:", all(b == (a // 2 if a % 2 == 0 else 3 * a + 1) for a, b in zip(t, t[1:])))
print("ENDS_AT_ONE:", t[-1] == 1)
