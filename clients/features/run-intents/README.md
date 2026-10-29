# Run intent controls

Mount `RunIntents` from `@vibesys/feature-run-intents/renderer` in the desktop
shell's new Run screen composition, beside the Run screen rather than inside
`feature-run-page`. Key it by host and run id. Supply:

- `bridge`: `window.vibesysGateway`, the existing generic unary/stream bridge.
- `host`, `runId`, and `projectRoot`: the selected run and its project directory.
  In the current shell, `page.state.record.run.projectRoot` is available when
  the record is `live` or `ended`. Pass that host path without expanding it on
  the laptop.
- `enabled`: whether the controls are active; the shell always passes true.
- `schedule`: the shell's injected timeout scheduler.

The component makes no calls and renders nothing while disabled. Electron main
fills HostCall's checkout from the host's current saved configuration. The
feature never chooses an executable. Its actor cancels every owned call when
unmounted or disabled. The legacy controls stay on the legacy path.

Pause, Pause now, Resume and Guidance are the visible run controls. The typed host
surface also accepts withdraw, cancel-attempt, cancel-item, and set-parallelism.
The command uses the existing inbox's validation, posting order, idempotence,
local-run guard, and ended-run rules. Retrying after an uncertain result sends
the identical request id and payload. A successful HostCall means durable
admission; the UI stays pending until an IntentState record says applied or
refused. A reader that has not declared the ordered inbox format is shown with
a posting-order warning, as the legacy CLI does. The feature follows Tail for those outcomes without
changing the skeleton projection.

## Relaunch

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

## Current limits

Chat messages use a separate session inbox and HostCall. The shell submits a
chat proposal through this feature's `useRunIntents` actor, preserving the same
intent admission and outcome handling as the controls.
The journal's [control projection](../../../src/vibesys/records/_control.py)
produces IntentState from recorded intent outcomes. A lifecycle record without
a matching request id does not complete a pending request. The consumer handles
an outcome even when it arrives before the admission response.
