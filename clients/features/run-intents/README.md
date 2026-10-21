# Run intent controls

Mount `RunIntents` from `@vibesys/feature-run-intents/renderer` in the desktop
shell's new Run screen composition, beside the Run screen rather than inside
`feature-run-page`. Key it by host and run id. Supply:

- `bridge`: `window.vibesysGateway`, the existing generic unary/stream bridge.
- `host`, `runId`, and `projectRoot`: the selected run and its project directory.
  In the current shell, `page.state.record.run.projectRoot` is available when
  the record is `live` or `ended`. Pass that host path without expanding it on
  the laptop.
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
refused. A reader that has not declared the ordered inbox format is shown with
a posting-order warning, as the legacy CLI does. The feature follows Tail for those outcomes without changing the
skeleton projection.

## Current limits

Chat messages use a separate session inbox and HostCall. The shell submits a
chat proposal through this feature's `useRunIntents` actor, preserving the same
intent admission and outcome handling as the controls.
The journal's [control projection](../../../src/vibesys/records/_control.py)
produces IntentState for steer and guidance delivery events. Lifecycle controls
publish lifecycle records without request ids, so those requests stay pending
unless the run emits a matching IntentState. The consumer handles an outcome
even when it arrives before the admission response.

There is no safe existing update-checkout or Launch/relaunch host command to
reuse. They are absent from HostCall rather than changing a checkout or starting
a process under new policy. The gateway requires the configured checkout's
synchronized `.venv/bin/vibesys`; a missing executable returns a typed diagnostic.
