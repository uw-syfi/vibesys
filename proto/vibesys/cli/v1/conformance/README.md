# Conformance corpus for the daemon contract

Files that two programs read and agree on, so the contract has one definition of what they say.
A file is read by the `cli` module's tests against the real server. A second reader (the
orchestrator's fake daemon) replays the same files without importing the first.

| File | Says | Read by the cli module in |
| --- | --- | --- |
| `protocol.json` | `watch_task_since`: the protocol version that added `WatchTask`. A client that needs the stream asks for at least it. | `internal/core` (`TestTheProtocolConstantsAreTheConformanceFile`) |
| `locate.golden.json` | Where the daemon's socket is for an environment and a user id. | `internal/runtimedir` (`TestLocateIsTheGolden`) |
| `task_watch/*.json` | A scenario for `WatchTask`: what happens to a task, and the exact messages a stream sends. | `internal/daemon` (`TestTaskWatchConformance`) |

## `task_watch` scenarios

```json
{
  "description": "what the scenario shows",
  "judged_commits": 0,
  "setup": [ {"op": "measure", "commit": "c0", "samples": "level"} ],
  "ops": [ {"op": "create_plan", "title": "t", "body": "b"} ],
  "cuts": [3],
  "expect": [ [ ...messages of the first stream... ], [ ...messages of the stream after the cut... ] ]
}
```

- `setup` happens before the stream opens. `ops` happen after it, each when the stream has said
  everything about the one before.
- `cuts` are counts of messages of the stream: the stream is cut after that many and a new one
  opens, which starts with a `Reset` (protocol 3 ignores a resume token).
- `expect` is the messages of each stream as `protojson`, with the epoch written `E1` and a commit
  written as `c<n>`. A message is one of `{"reset": {}}`, `{"facts": ...}` and `{"caughtUp": {}}`, with
  `sequence`.

Commits are `c0`, `c1`, ... in the scenario; `samples` name a set: `level` (about 100), `win`
(about 120) and `lose` (about 80). The ops are: `measure` (setup only), `promote`, `create_plan`,
`comment_plan`.

A scenario changes the task one commit at a time and waits for the stream between ops, so the
messages are the same on every run.
