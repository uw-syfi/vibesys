"""Accuracy gate: count_primes must match known prime counts."""

import sys

from primes import count_primes

KNOWN = {0: 0, 2: 0, 3: 1, 10: 4, 100: 25, 1000: 168, 3000: 430}

for limit, expected in KNOWN.items():
    got = count_primes(limit)
    if got != expected:
        sys.exit(f"count_primes({limit}) = {got}, expected {expected}")
