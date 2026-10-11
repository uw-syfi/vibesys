# Count primes faster

`primes.py` defines `count_primes(limit)`, the number of primes below `limit`.
It uses trial division by every smaller number, so it is slow.

Raise the benchmark's `calls_per_s` (calls of `count_primes(3000)` per second).
`count_primes` must keep returning exactly the same values; the accuracy check
compares it against known counts. Keep the function signature unchanged and use
only the Python standard library.
