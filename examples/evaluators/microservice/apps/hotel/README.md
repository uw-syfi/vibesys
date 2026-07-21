# Hotel benchmark adapter

This package defines typed benchmark traffic for DeathStarBench's Hotel
Reservation application. Search and recommendation responses are checked as
strict GeoJSON against an independently encoded copy of the seeded profile and
recommendation catalogs. Login responses require exact messages.

`reserve_capacity` reserves the complete seeded capacity for a trial-unique
future date, then verifies that one additional room is rejected. Reservation
plans are serialized and workloads must use one repetition because the upstream
application has no reservation deletion API.
