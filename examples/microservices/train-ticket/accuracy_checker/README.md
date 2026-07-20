# Train Ticket Accuracy Checker

This black-box checker validates the six mutable Train Ticket v0.2.0 services
through their public HTTP APIs. It makes no assumptions about process topology,
implementation language, database, or persistence format.

Every run uses a fresh cryptographically random namespace, randomized values,
and shuffled operation order. It verifies:

- the exact v0.2.0 startup catalog and response schemas;
- welcome, list, point-read, and secondary-index routes;
- connected station, train, route, price, and trip graphs;
- create, immediate read-your-write, update, and delete behavior;
- removal of stale station-name indexes after updates and deletes; and
- persistent HTTP connections.

Against an already running candidate:

```bash
python checker.py --base-url http://localhost:8080
```

Crash recovery is checked only when the checker owns the candidate lifecycle or
is given an external restart hook. The structured result reports
`crash_recovery: false` when no restart mechanism is provided; it never claims
that unexecuted property.

To let the checker start and `SIGKILL` a local candidate, provide its command as
a JSON argument. The checker starts the command in its own process session,
kills the entire process group, verifies that it exited, and reuses the same
`TRAIN_TICKET_DATA_DIR` after restart:

```bash
python checker.py \
  --base-url http://127.0.0.1:18080 \
  --candidate-dir /path/to/candidate \
  --run-command-json '["./run.sh"]'
```

For an externally managed deployment, use `--restart-command-json`. Separate
service addresses can be supplied by repeating `--target NAME=URL` for
`config`, `station`, `train`, `travel`, `route`, and `price`.

The exit status is zero only when every property actually exercised passes.

## Testing

```bash
uv run ruff check examples/microservices/train-ticket/accuracy_checker
uv run pytest -q examples/microservices/train-ticket/accuracy_checker/tests
```
