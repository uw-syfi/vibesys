# Campaign workspace UI

The campaign workspace from `adi/campaign-workspace-ui` (source commit
`8b74c0e8c`) is ported onto the client package architecture in `main` at
`3a48021f3`. The source branch remains available for the original design history.

## Preserved behavior

The default browser page is the recorded campaign preview: objective brief and
objective details, performance trajectories and measurement scrubbing, current
workstreams, workstream and agent activity timelines, searchable and sortable
history, and workstream/agent detail sheets. Wide screens keep current work,
performance, and selected measurement side by side; narrow screens reflow.
Timeline zoom and pan do not change the selected measurement. Detail navigation
preserves the replay position and keyboard focus.

Validated architecture captures may also project onto that same measurement
cursor. Stable component identities, deterministic layout, change highlights,
and optional profiler attribution are defined in the
[campaign architecture graph](architecture-graph-contract.md); the browser does
not infer missing topology or measurements.

The Connect action uses the current gateway connection feature. Live runs keep
main's event bootstrap, reconnect, stream/control banners, iteration navigation,
run commands, and server-selected themes. A Conversation action adds the branch's
Ask and Steer modes. Ask uses `query.chat`; returned events fold into the existing
run store. Steer uses `command.steer`. Preview pages omit this action because they
have no live run. Drafts and pending requests belong to their run session.

## Ownership

- `clients/features/campaign` owns the campaign parser, frame fold, replay and
  streaming state machines, dashboard, timeline, and presentation styles.
  `CampaignViewModel` remains the presentation boundary.
- `clients/features/conversation` owns the run conversation machine and view.
- `clients/libs/run-session` owns the typed chat operation on the existing run
  connection; it does not create a second control transport or transcript fold.
- `clients/apps/web` composes these features with the current gateway and run
  features. Its platform backend owns HTTP and SSE. The Vite plugin owns the
  development preview's SSE producer.

Features have public `state` and `renderer` entries. Apps compose peers through
these interfaces; neither feature imports another feature. Data folds are pure;
XState owns control modes, cancellation, and playback timers. The app injects
fixture fetching and the campaign stream.

The branch's focused Radix components and Tailwind styles remain confined to the
campaign feature. Existing shared UI components serve the gateway and conversation.

## Compatibility decisions

The old `clients/web`, `backend-client`, and `core-state` packages are not restored.
Their behavior uses the current app, contracts, run-state, transport, and run-session
packages. Generated protocol artifacts are unchanged.

The branch's experimental `async_operation_lifecycle` backend patch is not ported:
main removed that lifecycle event. The old thread-based WebSocket helper is also
superseded by main's owned blocking-request implementation and shutdown tests.
The campaign's preview frames are separate from the production run protocol; see
[live-campaign-streaming.md](live-campaign-streaming.md).
