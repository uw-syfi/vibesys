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
