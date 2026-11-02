package app

import (
	"bytes"
	"context"
	"errors"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// execute carries out one request of the machine and answers it with the event that says how it
// went. Every request is answered, whatever happens: an unanswered request would hold the
// machine for good.
func (a *App) execute(r lifecycle.Request) {
	switch r := r.(type) {
	case lifecycle.LaunchRequest:
		a.launch(r.Ref)
	case lifecycle.Adopt:
		a.adopt(r.Ref)
	case lifecycle.SignalCancel:
		a.signalCancel(r)
	case lifecycle.Move:
		a.move(r)
	case lifecycle.Discard:
		a.discard(r)
	}
}

// launch starts the supervisor of a queued job and records the job as running. The order is the
// point: the supervisor is up and parked first, the store says running second, and the command
// starts third. A crash before the second step leaves a queued job and a parked supervisor,
// which the next start discards; a crash after it leaves a running job whose supervisor the next
// start adopts and starts.
func (a *App) launch(ref lifecycle.Ref) {
	h, err := a.prepare(ref)
	if err != nil {
		a.dispatch(lifecycle.LaunchFailed{Ref: ref, Failure: failureOf(err), At: a.now()})
		return
	}
	a.dispatch(lifecycle.Launched{Ref: ref})
	a.follow(ref, h)
}

// prepare does the steps of a launch up to and including recording the job as running. It
// returns with nothing left behind when it fails.
func (a *App) prepare(ref lifecycle.Ref) (supervisor.Handle, error) {
	t, err := a.taskOf(ref.Store)
	if err != nil {
		return nil, err
	}
	job, err := t.store.Job(a.ctx, ref.Job)
	if err != nil {
		return nil, err
	}
	spec := job.Spec
	if spec.Environment == core.EnvironmentAuto {
		spec.Environment = a.cfg.DefaultEnvironment
	}
	if c, ok := spec.Settings.(core.CommandSettings); ok && c.Timeout <= 0 {
		c.Timeout = a.cfg.DefaultTimeout
		spec.Settings = c
	}
	env, err := a.cfg.Environments.Environment(spec.Environment)
	if err != nil {
		return nil, err
	}
	fingerprint, err := env.Fingerprint(a.ctx)
	if err != nil {
		return nil, err
	}
	launch := lifecycle.Launch{Repository: t.scope.Repository, Task: t.scope.Task, Job: job.ID, Spec: spec}
	if spec.Settings.Kind() != core.JobCommand {
		plan, err := a.planFlow(t, job, fingerprint)
		if err != nil {
			return nil, err
		}
		launch.Plan = &plan
	}
	h, err := a.cfg.Supervisors.Spawn(a.ctx, launch)
	if err != nil {
		return nil, err
	}
	a.setHandle(ref, h)
	if _, err := t.store.Transition(a.ctx, core.Transition{
		Job: job.ID, From: core.JobQueued, To: core.JobRunning, At: a.now(), Environment: &fingerprint,
	}); err != nil {
		a.release(ref)
		return nil, err
	}
	a.notifyJobs()
	return h, nil
}

// adopt follows the job of a supervisor that an earlier daemon started.
func (a *App) adopt(ref lifecycle.Ref) {
	h := a.handleOf(ref)
	if h == nil {
		a.dispatch(lifecycle.SupervisorLost{Ref: ref, At: a.now()})
		return
	}
	a.follow(ref, h)
}

// follow lets the supervisor run the command if it has not yet, moves the job's output into its
// store as it appears, and tells the machine how the job ended. It returns when the machine has
// been told, or when the daemon stops, in which case the next start picks the job up again.
func (a *App) follow(ref lifecycle.Ref, h supervisor.Handle) {
	t, err := a.taskOf(ref.Store)
	if err != nil {
		a.cfg.Logf("job %s: %v", ref, err)
		return
	}
	if err := h.Start(a.ctx); err != nil {
		a.supervisorFailed(ref, "starting the job", err)
		return
	}
	offset, err := logSize(a.ctx, t.store, ref.Job)
	if err != nil {
		a.cfg.Logf("job %s: reading its stored output: %v", ref, err)
		return
	}
	outcome, err := h.Follow(a.ctx, offset, func(data []byte) error {
		if err := t.store.AppendLog(a.ctx, ref.Job, data); err != nil {
			return err
		}
		a.logsChanged.Notify()
		return nil
	})
	if err != nil {
		a.supervisorFailed(ref, "following the job", err)
		return
	}
	a.dispatch(lifecycle.Finished{Ref: ref, Outcome: outcome, At: a.now()})
}

// supervisorFailed handles a call to a supervisor that did not work. A supervisor that is gone
// ends its job as lost, and never retried. The daemon being stopped is not the job's end. Any
// other failure, such as the store refusing the job's output, is not known to be the job's end,
// so the job is left running in its supervisor and the next start takes it up again.
func (a *App) supervisorFailed(ref lifecycle.Ref, doing string, err error) {
	switch {
	case a.ctx.Err() != nil:
	case errors.Is(err, supervisor.ErrGone):
		a.dispatch(lifecycle.SupervisorLost{Ref: ref, At: a.now()})
	default:
		a.cfg.Logf("job %s: %s: %v; the job stays running until the daemon restarts", ref, doing, err)
	}
}

func (a *App) signalCancel(r lifecycle.SignalCancel) {
	h := a.handleOf(r.Ref)
	if h == nil {
		return
	}
	if err := h.Cancel(a.ctx, r.Reason); err != nil && !errors.Is(err, supervisor.ErrGone) && a.ctx.Err() == nil {
		a.cfg.Logf("job %s: asking its supervisor to cancel: %v", r.Ref, err)
	}
}

// move records the end of a job: its explanation in the output first, so that a reader who sees
// the job final sees the line, then the transition, then the release of the supervisor. A
// transition that fails leaves the supervisor in place with the outcome, for the next start.
func (a *App) move(m lifecycle.Move) {
	defer a.dispatch(lifecycle.Closed{Ref: m.Ref})
	t, err := a.taskOf(m.Ref.Store)
	if err != nil {
		a.cfg.Logf("job %s: %v", m.Ref, err)
		return
	}
	if m.Settlement.Log != "" {
		if err := appendLineOnce(a.ctx, t.store, m.Ref.Job, m.Settlement.Log); err != nil {
			a.cfg.Logf("job %s: adding a line to its output: %v", m.Ref, err)
		}
	}
	if _, err := t.store.Transition(a.ctx, m.Settlement.Transition); err != nil {
		// A replay finds the job settled already.
		if job, jerr := t.store.Job(a.ctx, m.Ref.Job); jerr != nil || !job.State.Final() {
			if a.ctx.Err() == nil {
				a.cfg.Logf("job %s: recording its end: %v; the next start records it again", m.Ref, err)
			}
			return
		}
	}
	a.notifyJobs()
	if m.Release {
		a.release(m.Ref)
	}
}

func (a *App) discard(d lifecycle.Discard) {
	a.release(d.Ref)
	a.mu.Lock()
	queued := a.discarded[d.Ref]
	delete(a.discarded, d.Ref)
	a.mu.Unlock()
	if queued {
		a.dispatch(lifecycle.Discarded{Ref: d.Ref})
		return
	}
	a.dispatch(lifecycle.Closed{Ref: d.Ref})
}

// release lets go of a job's supervisor and removes what is left of its run. It is for a job
// whose end is recorded or that never ran.
func (a *App) release(ref lifecycle.Ref) {
	if h := a.handleOf(ref); h != nil {
		if err := h.Release(a.ctx); err != nil && !errors.Is(err, supervisor.ErrGone) && a.ctx.Err() == nil {
			a.cfg.Logf("job %s: releasing its supervisor: %v", ref, err)
		}
		a.dropHandle(ref)
	}
	if err := a.cfg.Supervisors.Forget(a.ctx, ref); err != nil && a.ctx.Err() == nil {
		a.cfg.Logf("job %s: removing its run: %v", ref, err)
	}
}

// logChunk is how much of a job's output is read at a time.
const logChunk = 1 << 20

// logSize returns how many bytes of a job's output the store holds.
func logSize(ctx context.Context, st store.Store, job core.JobID) (uint64, error) {
	var offset uint64
	for {
		chunk, err := st.ReadLog(ctx, job, offset, logChunk)
		if err != nil {
			return 0, err
		}
		if len(chunk.Data) == 0 {
			return offset, nil
		}
		offset = chunk.Next
	}
}

// appendLineOnce adds a line to the end of a job's output, unless the output already ends with
// it, which is what a replay of the same settlement finds. A line starts on a line of its own.
func appendLineOnce(ctx context.Context, st store.Store, job core.JobID, line string) error {
	text := []byte(line + "\n")
	size, err := logSize(ctx, st, job)
	if err != nil {
		return err
	}
	if size >= uint64(len(text)) {
		tail, err := st.ReadLog(ctx, job, size-uint64(len(text)), len(text))
		if err != nil {
			return err
		}
		if bytes.Equal(tail.Data, text) {
			return nil
		}
	}
	if size > 0 {
		last, err := st.ReadLog(ctx, job, size-1, 1)
		if err != nil {
			return err
		}
		if !bytes.Equal(last.Data, []byte("\n")) {
			text = append([]byte("\n"), text...)
		}
	}
	return st.AppendLog(ctx, job, text)
}
