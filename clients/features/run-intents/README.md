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

Pause, resume, stop (drain), park, and guidance are visible controls. The typed host
surface also accepts withdraw, cancel-attempt, cancel-item, and set-parallelism.
The command uses the existing inbox's validation, posting order, idempotence,
local-run guard, and ended-run rules. Retrying after an uncertain result sends
the identical request id and payload. A successful HostCall means durable
admission; the UI stays pending until an IntentState record says applied or
refused. A reader that has not declared the ordered inbox format is shown with
a posting-order warning, as the legacy CLI does. The feature follows Tail for those outcomes without changing the
skeleton projection.

## Current limits

Chat-message requests are absent.
The journal's [control projection](../../../src/vibesys/records/_control.py)
produces IntentState for steer and guidance delivery events. Lifecycle controls
publish lifecycle records without request ids, so those requests stay pending
unless the run emits a matching IntentState. The consumer handles an outcome
even when it arrives before the admission response.

Relaunch shares the header action row and result status line. The shell passes
the latest epoch and that epoch's lifecycle as the optional `relaunch`
observation, independently of timeline scrubbing. The feature watches
`WatchRuns` for that run's liveness, fenced by the writer epoch. It offers
Relaunch for parked, stopped-resumable, or interrupted observations and hides
it for an active writer or a permanently ended run.

The actor retains an in-flight request and its result when Tail observes the
new epoch before HostCall replies. A new eligible epoch enables the next
operator relaunch. An unconfirmed call is never retried automatically.
The existing skeleton fold supplies the timeline boundary.

The app has no safe checkout update operation. Update the host checkout
before relaunching; the new epoch records its tree hash and dirty flag.
The gateway requires the checkout's synchronized `.venv/bin/vibesys`; an
absent executable returns the checkout-missing refusal.
