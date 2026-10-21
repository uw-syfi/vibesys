# Run chat sessions

The desktop shell mounts `useRunSessions` beside the Run screen and
`useRunIntents`. Pass its `open`, `toggle`, and `panel` to the screen's chat
slot. Key the composition by host and run id. Supply the generic gateway
bridge, host, run id, project root, timeout scheduler, the per-host transport
switch, and session id (`default` for the default conversation).

The disabled feature owns no calls. Opening it tails the session log through
`Tail` with a session selector. Closing it cancels its reads and outstanding
admission calls; reopening resumes from the last folded sequence. Session
history belongs to the run and survives both session exit and run relaunch.
Large answers load through the existing `GetBlobs` call.

`sendSteer` is an application callback. The shell converts the proposal to a
steer request and submits it to the same `useRunIntents` actor that owns the
run controls. The chat feature never submits an intent itself. The intent
controls show its durable admission and eventual applied/refused outcome.

The machine owns message admission, bounded content reads, reconnects and
cancellation. An uncertain message can be retried explicitly with its original
request id and payload. The fold owns ordered history, replaces streamed text
with the final answer, and ignores replayed records.
