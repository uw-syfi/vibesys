# Train Ticket Accuracy Checker

Checks a running Train Ticket deployment through the API gateway. The checker
uses only read-only endpoints by default:

- service welcome endpoints
- station, train, travel, route, price, config, and contact list endpoints

Usage:

```bash
python checker.py --base-url http://localhost:8080
```

Use `--allow-empty` for deployments without seeded data.
