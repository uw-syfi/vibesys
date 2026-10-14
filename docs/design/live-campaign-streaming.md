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
idempotent. The cursor follows the tail until the user scrubs, while run status
continues to come from the stream. Selecting the tail resumes following.
The subscriber owns the SSE connection and closes it when released. A closed
connection or malformed frame surfaces an error; a later valid frame clears it.
A browser reconnect may replay earlier frames without duplicating state.

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
