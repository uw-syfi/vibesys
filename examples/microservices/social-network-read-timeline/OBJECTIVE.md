# Social Network: Read Heavy Scenario (Issue #48)

## Deployment Goal

Optimise DeathStarBench's socialNetwork application for minimum p50 read latency
on a local server under a read-heavy mixed workload, while preserving correctness
as verified by accuracy_checker/checker.

## Target System

DeathStarBench socialNetwork — Go microservices stack (nginx-thrift frontend,
ComposePost, UserTimeline, HomeTimeline, SocialGraph, PostStorage, UserService,
UserMention, UrlShorten, UniqueId, Media services), MongoDB + Redis + Memcached,
Thrift RPC internally, deployed via Docker Compose.
Repo: https://github.com/delimitrou/DeathStarBench/tree/master/socialNetwork

## Workload

Read-heavy mixed load against nginx on port 8080:
- 50% user-timeline reads: GET /wrk2-api/user-timeline/read
- 40% home-timeline reads: GET /wrk2-api/home-timeline/read
- 10% compose + user-timeline read-your-write sequences (keeps content fresh
  and rejects acknowledged writes that do not become visible)

## Hardware Target

Local server. x86-64 or Apple Silicon. Docker required.

## Interface

HTTP on port 8080. Wire-compatible with DeathStarBench socialNetwork nginx API.
`accuracy_checker/checker` and the shared evaluator workload can point at any
candidate without modification.

## Optimisation Objective

Minimise **p50 combined read latency** (`p50_ms` from benchmark output).
`success_rate` must stay at 1.0, every workload operation must be exercised,
measured timeline responses must preserve schema/order/identity semantics, and
all 11 standalone correctness checks must pass.

Key optimisation directions which could be explored:
- Redis hit rate for UserTimeline and HomeTimeline services
- Memcached hit rate for PostStorage
- gRPC/Thrift connection pooling in nginx GenericObjectPool
- Go runtime tuning (GC%, goroutine pool sizing)
- UserTimeline and HomeTimeline service query optimisation
