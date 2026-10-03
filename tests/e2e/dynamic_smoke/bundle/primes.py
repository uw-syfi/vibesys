"""Prime counting, the function this smoke task optimizes."""


def count_primes(limit: int) -> int:
    """Return how many primes are below ``limit``."""
    count = 0
    for candidate in range(2, limit):
        divisors = 0
        for divisor in range(2, candidate):
            if candidate % divisor == 0:
                divisors += 1
        if divisors == 0:
            count += 1
    return count
