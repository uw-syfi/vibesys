# Pareto frontier

Configured axes: throughput:max
Dominance is variance-aware: a point removes another only when it is no worse within 0% on every axis and better by more than 0% on at least one.
Input baseline (measured before round 1), commit fake-revisio: throughput=100 (max). A round is retained only if it also beats this.
Latest completed round: round 1, commit fake-revisio, official metrics: throughput=120 (max); retained: True.
Trusted frontier parents:
- round 1, commit fake-revisio, official: throughput=120 (max); operating point: canonical workload row; artifact: (missing)
