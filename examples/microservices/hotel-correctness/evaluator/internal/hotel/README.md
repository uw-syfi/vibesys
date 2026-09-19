# Hotel accuracy oracle

The Hotel accuracy adapter owns an independent compatibility oracle for the
pinned DeathStarBench Hotel Reservation API. Existing targeted checks validate
strict response schemas, seed catalogs, negative requests, state transitions,
and managed crash recovery.

`hotelOracle` is the canonical application-specific reference behavior. Given a
logical `hotelState` and one `differentialAction`, its `Step` method returns the
required observation and successor state. Its `AfterCrash` method preserves
acknowledged reservations. The oracle does not issue requests or schedule
events.

Generators produce shared `accuracy.Program` values. A program contains calls,
parallel call groups, and crash/start events, but no expected results. The
Hotel candidate adapter translates call actions into HTTP requests. The shared
verifier schedules those events, records a replayable trace, and accepts an
execution exactly when the observations match some execution admitted by
`hotelOracle`.

The model preserves externally observable reference behavior, including
capacity by hotel and night, atomic over-capacity rejection, search visibility,
hotel/date isolation, and the pinned frontend behavior where an invalid-login
reservation may mutate capacity before returning the authentication failure.
GeoJSON object and feature order are normalized, while schemas, profile values,
messages, and result membership remain exact.

This package does not model internal gRPC calls, databases, caches, timing, or
service topology.

## Event programs

Sequential differential, concurrency, and durability checks use the same
reference behavior and request adapter. Each generator owns a disjoint slice of
the seeded date namespace (see namespaces.go), because the public API has no
delete operation. Hotel accuracy accepts at most 25 cases; larger runs would
make the per-case multi-night range overlap the fixed endpoint-liveness range.
The default remains four required cases plus up to three randomized extras.

| Property | What it asserts |
| --- | --- |
| `sequential_differential` | A generated sequence of login, recommendation, search, and reservation calls matches the canonical state machine. |
| `degenerate_date_ranges` | A range enclosing no nights (`inDate == outDate`, or reversed) is acknowledged and consumes nothing. |
| `multi_night_atomicity` | A span over a full interior night is rejected whole, and the flanking nights keep their rooms. |
| `concurrent_isolation` | Simultaneous single-room reservations on distinct hotels all succeed and each consumes exactly one room. |
| `crash_recovery` | A filled hotel remains full across explicit crash and start events. |
| `durable_capacity` | Acknowledged reservations remain visible to the same model after program crash/start events. |

The shared verifier checks a parallel group against legal serializations of the
ordinary `hotelOracle.Step` transition. Contention fills a night to `k` remaining
rooms, then issues between six and eight simultaneous single-room requests.
Every legal serialization acknowledges exactly `k`. Follow-up calls pin the
resulting capacity through the same oracle rather than a separate concurrency
predicate.

Durability programs place crash and start between acknowledged writes and
readback calls. The managed lifecycle hooks perform the real process events;
`hotelOracle.AfterCrash` defines which logical state must survive. Search and
reservation readbacks are ordinary actions judged by `Step`.

## Opt-in properties the pinned reference violates

Three properties are declared but only checked when a workload asks for them,
because unmodified DeathStarBench fails all three. Turning one on rejects the
reference implementation, not just a regressed candidate.

```toml
[application_config]
strict_linearizable_capacity = true
strict_durable_availability = true
strict_endpoint_liveness = true
```

| Property | Upstream behavior |
| --- | --- |
| `linearizable_capacity` | `MakeReservation` reads the room count from memcached, releases it, and writes back after the capacity test, so simultaneous requests oversell. |
| `durable_availability` | `CheckAvailability` serves availability from cached counts; its datastore miss path is unreachable, so a full hotel is advertised again once the cache tier restarts. |
| `endpoint_liveness` | The capacity lookup calls `log.Panic` when no seeded row matches, so one request for an out-of-catalog hotel takes the reservation service down. |

Issue [#254](https://github.com/uw-syfi/vibesys/issues/254) records the measured
counterexamples, the exact commands, and what they imply for using this oracle
as an accuracy gate.

## Scope

Interleaved read/write coherence inside one parallel group, in-flight requests
interrupted by a crash, and multi-key transactional histories remain
unmodeled. Version 1 programs permit lifecycle events only at quiescent step
boundaries and bound parallel groups to eight calls.
