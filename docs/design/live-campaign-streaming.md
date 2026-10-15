# Campaign stream preview

The campaign dashboard supports both the committed trajectory record and a
sequence of validated campaign frames. This is a development preview contract,
not a claim that the production backend publishes campaign frames.

Run `pnpm --dir clients/apps/web dev` and open `/?campaign-live`. The optional
`interval` query parameter controls frame pacing in milliseconds (20–5000; the
default is 350). Vite serves `/__vibesys/campaign/stream` only in development.
Its producer validates the committed fixture and calls `framesFromRecord` from
`@vibesys/feature-campaign/state`. The browser's platform backend validates each
SSE message with `parseCampaignFrame`; the feature folds it into campaign state.

The closed frame union consists of `campaign-init`, `agent-upsert`, `workstream-upsert`,
`measurement`, `tokens`, and `status`. Sequence numbers make repeated frames
idempotent only when the repeated payload is identical. The pure fold retains a
canonical identity for each frame in the accepted prefix, so reusing an old
sequence with different data is an integrity error rather than a silent replay.
The identity ledger grows with that preview stream because evicting an old
identity would make strict reconnect verification impossible without a
producer-supplied digest chain. The first frame is `campaign-init` at sequence
one; later applied frames are contiguous, another init is rejected, and
completion is absorbing.
The cursor follows the tail until the user scrubs, while run status continues to
come from the stream. Selecting the tail resumes following. The subscriber owns
the SSE connection and closes it when released or when the campaign completes.
A closed connection or malformed frame surfaces an error. A newly applied frame
can clear a transport error, but a duplicate cannot. An integrity error closes
that subscription and remains visible because no later delta can prove that the
accumulated prefix is authoritative. A browser reconnect may replay earlier
frames without duplicating state.

The committed campaign record does not contain token usage. `framesFromRecord`
therefore emits no `tokens` frames, and the preview omits the Tokens column
rather than deriving plausible-looking totals from workstream order or sequence
span. The `tokens` frame remains in the preview contract so an authoritative
producer can supply cumulative per-workstream usage when that data exists.

## Live session-derived stream

A third source joins the committed-fixture replay and the dev-only fixture/SSE preview above:
`webSessionCampaignStream` (`clients/features/campaign/src/state/live-campaign-stream.ts`) projects
a real run's already-folded `CoreState` into the same frame union, live, in both the browser and
desktop apps' `Live` page (`clients/apps/web/src/app/compose.tsx`) — not behind the `campaign-live`
dev gate, and not a Vite plugin. `compose.tsx`'s `campaignChrome` distinguishes the three by the
label a caller passes: `'Preview'` for the committed-fixture replay, `'Stream preview'` for the
dev-only SSE path above, and no label for this live session-derived stream.

The data it folds comes from two wire events added for this purpose, `WORKSTREAM_FINISHED` and
`TURN_USAGE_RECORDED` (`src/vibesys/events/`), joined to a workstream via `round_label` since
neither event payload carries its own workstream identity. This closes the gap this preview
contract could not close before: the committed campaign record (and therefore `framesFromRecord`)
carries no token usage at all, so replay never emitted `tokens` frames. The live stream can, from
real per-turn usage, but still cannot derive `objective`, `benchmarkVersions`, or `trajectories`
from the run protocol as it exists today — those fields stay empty on every frame this producer
emits. This is still a local, frontend-owned projection: the frame types are not a new backend API,
and a producer-side digest chain, persistence, or replay semantics for this live path do not exist.
The real answer for a backend-owned, versioned contract generated into TypeScript is tracked by
#1761; this stream is a step toward the data it would carry, not an implementation of it.

## Backend path

The campaign frame types remain a local preview contract. A production campaign
projection would need an authoritative backend contract and generated downstream
types, plus explicit persistence and replay semantics. The local workstream phase
union is not a new backend API. The original branch's async-operation lifecycle
proof of concept is obsolete after main's lifecycle changes and is intentionally
not revived by this port.

The production gateway-backed run page continues to use the existing generated
run protocol and the current session machine. Its conversation commands use the
existing chat and steer operations. No production campaign backend or new
orchestration behavior is introduced.
