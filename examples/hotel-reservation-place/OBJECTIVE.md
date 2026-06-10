# Hotel Reservation: Place Reservation

## Deployment Goal

Serve a transactional hotel reservation application that handles concurrent reservation
requests correctly, optimised for write-heavy workloads on a single commodity server.

## Application Parameters

The system maintains:
- Hotel profiles (name, location, star rating)
- Room inventory per hotel (room type, capacity, rate per night)
- User registry
- Reservation records (user, hotel, room, check-in/check-out dates)

## Workload

Transactional, write-heavy. A benchmark driver:
1. Initialises a fixed set of hotels, room types, and users via setup APIs.
2. Fires concurrent `POST /reservations` requests with overlapping date ranges.
3. Interleaves `GET /hotels/{id}/availability` and `GET /reservations/{id}` reads
   to verify visible state after writes.
4. Measures metrics: reservation success rate, overbooking violation count, p50/p95/p99
   latency, throughput, CPU usage, and memory usage.


## Interface

HTTP REST API — see `reference/config.json` for the exact endpoint contract.


## Optimisation Objective

Maximise **reservations/sec** under concurrent load while keeping `overbooking_violations` at zero.