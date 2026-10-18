# Iteration navigation contract

`vibesys.api` owns the versioned `IterationTree` projection. Policies produce
that typed projection from their durable state, the server pages direct
children without interpreting policy JSON, and clients normalize generated
protocol DTOs by stable ID. Existing `RoundSummary` and event-derived round
state remain compatibility contracts.

The built-in mappings are:

- `single-agent`, `profile-guided-single-agent`, `multi-agent`, and
  `profile-guided-multi-agent`: one root `round` node per recorded hypothesis
  round, ordered by recorded round number.
- `dynamic`: one root `workstream` node per planner `work_id`, with one
  `attempt` child per scheduled attempt. Root order is the first scheduler
  sequence for the workstream and child order is attempt scheduler sequence;
  completion order changes neither identity nor ordinal.
- `evolve`: root `generation` nodes containing `candidate` children. Candidate
  identity comes from the population member ID, not its position in a page.
- `plain`: one root `board` node containing `item` children from the durable
  issue snapshot already held by `IssueQueueStrategyState`. The tracker remains
  authoritative; the projector neither opens nor parses provider storage.

IDs, sibling ordinals, parent references, child counts, status vocabulary, and
headline metric bounds are validated before a `RunView` is published. The
tree's revision is a digest of the typed node content, so live projection,
restart, persisted reopen, and replay produce the same revision for the same
state.

`query.iterations` returns at most 200 roots or direct children. Its cursor is
opaque to clients and binds the projection revision, parent ID, and offset.
Malformed, stale, cross-parent, and out-of-range cursors fail explicitly.
Clients replace a sibling set on a null cursor and append only an exact cursor
continuation. Replaying the same page is idempotent; reusing an applied cursor
with different content, skipping a cursor, or continuing a stale revision is
rejected. Clients fetch the root pages at bootstrap and after an
`iterations_changed` invalidation, fold them into shared core navigation state,
and fall back to event-derived rounds when the server predates the query or the
policy returns typed unavailability. This preserves deterministic navigation
while keeping response size independent of total run history.
