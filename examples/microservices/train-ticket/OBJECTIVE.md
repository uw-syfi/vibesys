Optimize a Train Ticket microservice deployment for read-only API throughput
while preserving response correctness.

Headline metric: `requests_per_second` from the shared evaluator result
(`primary_value`, maximize).

The target is a running Train Ticket gateway or direct-service deployment. The
candidate must preserve the existing HTTP API behavior checked by
`accuracy_checker/checker.py`, including service welcome endpoints, list
response envelopes, expected item fields, and cross-service referential
integrity.

The benchmark exercises a fixed-rate read-only workload across station, train,
trip, route, price, config, and welcome endpoints. Improve successful completed
requests per second without increasing the error rate beyond the benchmark's
configured threshold.
