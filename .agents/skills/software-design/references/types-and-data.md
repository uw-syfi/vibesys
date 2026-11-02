# Types and data

Read this before you design a type, a state, or stored data (checkpoint
questions 1 and 2, rules 15 and 16). Do it before the code: types and data fix
the shape of everything written after, and changing them later touches every
consumer. Language idioms are in [python.md](python.md),
[typescript.md](typescript.md) and [go.md](go.md).

## Unrepresentable states

Flags and optional fields allow combinations that mean nothing:

```
Job { status: string, started_at?: Time, finished_at?: Time, error?: string }
```

Nothing stops `status = "done"` with no `finished_at`, or an `error` on a job
that is still running. Make the lifecycle a closed union whose variants carry
exactly what is true in that state:

```
Job = Queued | Running{started_at} | Succeeded{started_at, finished_at}
    | Failed{started_at, finished_at, error} | Unknown{last_seen}
```

Match it exhaustively, so a new state is a compile error in every consumer.
Give an ambiguous observation its own variant (see
[functional-core.md](functional-core.md)) instead of defaulting it to success.

## Proof types

An action that needs a check should take the check's result, not a flag the
caller promises to have set:

```
deploy(commit: String, verified: Bool)        # callers forget, or lie
verify(commit: Commit) -> Verified | Rejected # only verify builds Verified
deploy(proof: Verified)                       # cannot be called unchecked
```

Do the same for identifiers. A `Commit`, a `JobId` and a `Digest` are different
types parsed once at the boundary (rule 7), so a digest cannot be passed where
a commit is expected and a raw string is never validated twice.

## Sketching the data

Write one row per fact, before any code:

| Fact | Owner | Stored or derived | Read / written | Volume | Encoding |
| --- | --- | --- | --- | --- | --- |
| Job state | scheduler | stored | read often, written per transition | thousands | log record on disk, message on the wire |
| Queue depth | none | derived from job states | read per request | one number | not stored |

1. List the facts; give each exactly one owner (rule 8).
2. Mark which are derived, and derive them rather than storing a second copy.
3. For each stored fact, note how often it is read and written and how much
   there is; these choose the structure (in memory or on disk, append-only log
   or table, an index, a bound on growth).
4. Name the encoding at each boundary, its schema, and how it will change
   (rule 15). One schema is the source for every downstream type.

## Size the design to the change

- Adds no type, state or stored data: write "no new types or data".
- Adds a field, a variant or one stored fact: the changed type and the affected
  rows, in a few lines.
- A new subsystem: the full sketch before implementation, covering every fact
  row, each lifecycle union with its transitions, the proof values, the
  identifier types, and the encodings. Review it before the implementation is
  written, not after.

## Stop and redesign

A test or audit finding that a type could have ruled out is the sign. Examples:
a test that builds an invalid combination to check a guard rejects it; an audit
finding that something ran before its check; a comment saying a field is "only
set when" some status holds; the same check repeated at several call sites.
Do not patch the instance. Fix the type so the state cannot be built, and
delete the tests and checks that existed only to catch it.

Unenforced: deciding what becomes a type or a stored fact needs judgment. Once a
type exists, the compiler, the type checker and an exhaustiveness check hold it.
