# Train Ticket Accuracy Checker

Checks a running Train Ticket deployment using read-only endpoints:

- welcome endpoints of the config, station, train, travel, route, and price
  services (exact service banner text)
- station, train, trip, route, price, and config list endpoints

For every list endpoint the checker validates the full response contract, not
just HTTP reachability:

- the `edu.fudan.common.util.Response` envelope reports `status == 1`
  (an HTTP-200 body with `status: 0` is a service-level failure and fails the
  check, except for the tolerated empty case under `--allow-empty`)
- every returned item has the expected fields (restricted to field names that
  exist in both the 0.2.0 prebuilt images and the v1.0.0 source)
- referential integrity across services: trips and prices must reference
  existing routes and trains; on 0.2.0 image deployments trips must also
  reference existing stations and train types (v1.0.0 renamed those trip
  fields, so those two checks skip there)

Usage:

```bash
python checker.py --base-url http://localhost:18888 --direct-services
```

- `--base-url` points at the gateway or UI proxy. With `--direct-services`
  only its hostname is used and each check goes straight to the service's
  published port (required for the local prebuilt-image cluster, whose gateway
  cannot route).
- `--allow-empty` tolerates unseeded deployments: list endpoints may return
  the `status: 0, msg: "No content"` empty envelope. All other validation
  still applies. The images used by the local cluster self-seed on startup,
  so the flag is not needed there.

Exit code is 0 only if every check (including the cross-endpoint consistency
checks) passes.
