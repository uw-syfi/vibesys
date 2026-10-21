# Run intent controls

Mount `RunIntents` from `@vibesys/feature-run-intents/renderer` in the desktop
shell's new Run screen composition, beside the Run screen rather than inside
`feature-run-page`. Key it by host and run id. Supply:

- `bridge`: `window.vibesysGateway`, the existing generic unary/stream bridge.
- `host`, `runId`, and `projectRoot`: the selected run and its project directory.
- `enabled`: the host's `runTransportV2` setting.
- `schedule`: the shell's injected timeout scheduler.

The component makes no calls and renders nothing while disabled. Electron main
fills HostCall's checkout from the host's current saved configuration. The
feature never chooses an executable. Its actor cancels every owned call when
unmounted or disabled. The legacy controls stay on the legacy path.

Pause, resume, stop (drain), and guidance are visible controls. The typed host
surface also accepts withdraw, cancel-attempt, cancel-item, and set-parallelism.
The command uses the existing inbox's validation, posting order, idempotence,
local-run guard, and ended-run rules. Retrying after an uncertain result sends
the identical request id and payload. A successful HostCall means durable
admission; the UI stays pending until an IntentState record says applied or
refused. The feature follows Tail for those outcomes without changing the
skeleton projection.

## Current limits

The existing inbox has no park or chat-message request. Park is represented in
the typed command surface and refused before writing; chat messages are absent.
The current main branch does not produce IntentState records, so requests stay
pending until the run-side producer lands. The outcome consumer already handles
those records, including an outcome arriving before the admission response.

There is no safe existing update-checkout or Launch/relaunch host command to
reuse. They are absent from HostCall rather than changing a checkout or starting
a process under new policy. The gateway requires the configured checkout's
synchronized `.venv/bin/vibesys`; a missing executable returns a typed diagnostic.
