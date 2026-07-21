# Hotel Reservation mixed-workload optimization

Optimize DeathStarBench's `hotelReservation` application for maximum sustained
end-to-end logical operations per second on a local CPU server. Preserve the
public frontend behavior and seeded data semantics verified by the shared
microservice accuracy evaluator.

The target is the Go/gRPC Hotel Reservation stack at frontend port 5000. The
canonical mix follows DeathStarBench's workload proportions:

- 60% hotel search;
- 39% recommendations, split evenly across distance, rating, and price;
- 0.5% authenticated login; and
- 0.5% reservation capacity transitions.

The reservation operation is a stateful logical sequence: it fills a uniquely
namespaced hotel/date slot to its exact capacity and verifies that one
additional room is rejected. Business failures use HTTP 200, so every response
must pass application-level validation. The benchmark is valid only with zero
semantic errors and coverage of every operation type.

Promising directions include removing redundant rate/profile database work,
improving safe cache reuse, reducing gRPC and JSON overhead, reusing downstream
connections, and tuning service concurrency. Do not hard-code evaluator inputs,
weaken the checked availability semantics, or bypass authentication checks
exercised by the contract. Evaluator requests are seeded and fuzzed; solutions
must implement the behavior rather than recognize a fixed set of URLs.

The accuracy gate owns candidate startup and restart. Stop any manually started
Compose deployment before declaring a round ready for judging so the managed
lifecycle can prove both clean startup and persistence across a restart.
