package app

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/owned"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// startEffect starts carrying out one request of the machine. It is called by the loop, and what
// the effect needs of the loop's maps is handed to it, because the effect runs beside the loop.
// Every request is answered unless the App closes. Replies for a retired generation
// are dropped: the machine has already forgotten that store's requests. Otherwise an
// unanswered request would hold the machine for good. SignalCancel is answered only
// when it did not arrive, as the job's outcome answers it otherwise.
func (a *App) startEffect(r lifecycle.Request, scopes map[lifecycle.StoreID]core.Scope, handles map[lifecycle.Ref]supervisor.Handle, generation *effectGeneration) {
	ref, run := a.effectOf(r, scopes, handles)
	ctx := context.WithValue(named(a.ctx, fmt.Sprintf("%T %s", r, ref)), generationKey{}, generation)
	a.group.Go(func() { run(ctx) })
}

func (a *App) postEffect(ctx context.Context, m message) bool {
	m.generation, _ = ctx.Value(generationKey{}).(*effectGeneration)
	m.observation, _ = ctx.Value(observationKey{}).(*storeObservation)
	return a.post(m) == nil
}

// settleEffect preserves a caller's lifetime stamp while awaiting application.
func (a *App) settleEffect(ctx context.Context, m message) error {
	m.generation, _ = ctx.Value(generationKey{}).(*effectGeneration)
	m.observation, _ = ctx.Value(observationKey{}).(*storeObservation)
	return a.settle(m)
}

func (a *App) answer(ctx context.Context, e lifecycle.Event) bool {
	return a.postEffect(ctx, message{event: e})
}

type effectKey struct{}

func named(ctx context.Context, name string) context.Context {
	return context.WithValue(ctx, effectKey{}, name)
}

// EffectOf names the effect a context belongs to, and is empty outside one. Effects that start
// together make calls that can look alike, and the order they arrive in is the scheduler's: this
// is what tells them apart, for a simulation that must decide their order itself.
func EffectOf(ctx context.Context) string {
	name, _ := ctx.Value(effectKey{}).(string)
	return name
}

// effectOf returns the effect of a request, bound to what the loop holds now, and the job it is for.
func (a *App) effectOf(r lifecycle.Request, scopes map[lifecycle.StoreID]core.Scope, handles map[lifecycle.Ref]supervisor.Handle) (lifecycle.Ref, func(context.Context)) {
	switch r := r.(type) {
	case lifecycle.LaunchRequest:
		scope, known := scopes[r.Ref.Store]
		return r.Ref, func(ctx context.Context) { a.launch(ctx, r, scope, known) }
	case lifecycle.Adopt:
		scope, h := scopes[r.Ref.Store], handles[r.Ref]
		missing := a.leases.missing(r.Ref, r.Leases)
		return r.Ref, func(ctx context.Context) {
			if len(missing) > 0 {
				t := a.adoptLeases(ctx, r.Ref, missing)
				if !a.postEffect(ctx, message{taken: &t}) {
					a.giveBack(t.leases)
				}
			}
			a.follow(ctx, r, scope, h)
		}
	case lifecycle.SignalCancel:
		h := handles[r.Ref]
		return r.Ref, func(ctx context.Context) { a.signalCancel(ctx, r, h) }
	case lifecycle.Stop:
		h := handles[r.Ref]
		return r.Ref, func(ctx context.Context) { a.stopJob(ctx, r, h) }
	case lifecycle.Move:
		scope, known, h := scopes[r.Ref.Store], hasScope(scopes, r.Ref), handles[r.Ref]
		return r.Ref, func(ctx context.Context) { a.move(ctx, r, scope, known, h) }
	case lifecycle.Discard:
		h := handles[r.Ref]
		return r.Ref, func(ctx context.Context) { a.discard(ctx, r, h) }
	case lifecycle.ArmRetry:
		return r.Ref, func(ctx context.Context) { a.armRetry(ctx, r.Ref) }
	case lifecycle.Reexamine:
		scope, known := scopes[r.Ref.Store]
		return r.Ref, func(ctx context.Context) { a.reexamine(ctx, r.Ref, scope, known) }
	}
	// A request nobody carries out would hold its job for good, without a word.
	panic(fmt.Sprintf("the machine made a request no effect carries out: %T", r))
}

func hasScope(scopes map[lifecycle.StoreID]core.Scope, ref lifecycle.Ref) bool {
	_, ok := scopes[ref.Store]
	return ok
}

// errNoScope says the daemon does not know where a job's store is.
var errNoScope = &core.Failure{Kind: core.FailureUnavailable, Message: "the daemon does not know the job's store"}

// retry calls do until it works, fails for good, or the attempts run out, and returns its last
// error. It waits between attempts through the timers. After the App closes, it stops waiting.
func (a *App) retry(ctx context.Context, do func(ctx context.Context) error) error {
	return a.retryWhile(ctx, transient, do)
}

// retryWhile is retry with the caller saying which failures are worth another try.
func (a *App) retryWhile(ctx context.Context, again func(error) bool, do func(ctx context.Context) error) error {
	delay := a.cfg.Retry.First
	var err error
	for attempt := 0; ; attempt++ {
		err = do(ctx)
		if err == nil || ctx.Err() != nil || a.ctx.Err() != nil || !again(err) || attempt+1 >= a.cfg.Retry.Attempts {
			return err
		}
		if !a.sleep(ctx, delay) {
			return err
		}
		delay = min(delay*2, a.cfg.Retry.Longest)
	}
}

// sleep waits for d, and reports false when ctx or the App ends first.
func (a *App) sleep(ctx context.Context, d time.Duration) bool {
	fired, stop := a.cfg.Timers.Timeout(ctx, d)
	defer stop()
	select {
	case <-fired:
		return true
	case <-ctx.Done():
		return false
	case <-a.ctx.Done():
		return false
	}
}

// storeError says an error came from a job's store and not from its supervisor, which decides what
// comes of the job.
type storeError struct{ err error }

func (e storeError) Error() string { return e.err.Error() }
func (e storeError) Unwrap() error { return e.err }

// spawnError is a failure of Supervisors.Spawn: a supervisor that did not come up.
type spawnError struct{ err error }

func (e spawnError) Error() string { return e.err.Error() }
func (e spawnError) Unwrap() error { return e.err }

// launchCauseOf is causeOf for a failed launch. Only a supervisor that did not come up is tried
// again: a failure before Spawn, such as an environment with no fingerprint, fails the job at once.
func launchCauseOf(err error) lifecycle.Cause {
	cause := causeOf(err)
	var se spawnError
	if cause == lifecycle.CauseSupervisorSilent && !errors.As(err, &se) {
		return lifecycle.CauseUnprepared
	}
	return cause
}

// causeOf says who a failed step depends on. A store that does not answer is away, whatever the
// error looks like; only an answer that will not change is a refusal.
func causeOf(err error) lifecycle.Cause {
	var se storeError
	fromStore := errors.As(err, &se)
	var f *core.Failure
	refused := errors.As(err, &f) && refusal(f.Kind)
	switch {
	case fromStore && (errors.Is(err, store.ErrAway) || errors.Is(err, store.ErrNoStore) || !refused):
		return lifecycle.CauseStoreAway
	case refused:
		return lifecycle.CauseRefused
	}
	return lifecycle.CauseSupervisorSilent
}

func refusal(k core.FailureKind) bool {
	switch k {
	case core.FailureNotFound, core.FailureInvalidInput, core.FailureIncompatible, core.FailureConflict, core.FailureEnvironmentMismatch:
		return true
	}
	return false
}

// goneProof returns the proof in an error that a supervisor is gone. ErrGone alone holds none.
func goneProof(err error) (lifecycle.DeathProof, bool) {
	g, ok := goneOf(err)
	return g.Proof, ok
}

// goneOf returns the Gone in an error, with its proof, and false when it holds no proof.
func goneOf(err error) (supervisor.Gone, bool) {
	var g supervisor.Gone
	if errors.As(err, &g) && g.Proof.Valid() {
		return g, true
	}
	return supervisor.Gone{}, false
}

// errNoHandle says the daemon holds no connection to a job's supervisor, which proves nothing.
var errNoHandle = &core.Failure{Kind: core.FailureUnavailable, Message: "the daemon has no connection to the job's supervisor"}

// transient says trying again can help. A call whose own deadline passed can be tried again: the
// caller's context, which ends the trying, is looked at by retry.
func transient(err error) bool {
	if errors.Is(err, supervisor.ErrGone) {
		return false
	}
	var f *core.Failure
	return !errors.As(err, &f) || !refusal(f.Kind)
}

// call makes one call to a supervisor, which gets a bounded time to answer.
func (a *App) call(ctx context.Context, do func(ctx context.Context) error) error {
	return a.retry(ctx, func(ctx context.Context) error {
		ctx, done := clock.Deadline(ctx, a.cfg.Timers, a.cfg.CallTimeout)
		defer done()
		return do(ctx)
	})
}

// findStore finds a store, trying for a bounded time.
func (a *App) findStore(ctx context.Context, scope core.Scope) (store.Store, error) {
	var st store.Store
	err := a.retry(ctx, func(ctx context.Context) error {
		var err error
		st, err = a.cfg.Stores.Find(ctx, scope)
		return err
	})
	return st, err
}

// readJob reads a job, trying for a bounded time.
func (a *App) readJob(ctx context.Context, st store.Store, id core.JobID) (core.Job, error) {
	var job core.Job
	err := a.retry(ctx, func(ctx context.Context) error {
		var err error
		job, err = st.Job(ctx, id)
		return err
	})
	return job, err
}

// errMoved says a job was not in the state a transition expected, and the transition now expects
// the state it is in.
var errMoved = errors.New("the job moved")

// record applies a transition. A call that failed may have worked, as a reply can be lost, so what
// counts is the state the store shows afterwards: the end asked for, or any end, is recorded, and
// a job in a state the transition did not expect is moved on from there at once, in the same try:
// a try spent on learning the state would leave a daemon that tries once never moving the job.
func (a *App) record(ctx context.Context, st store.Store, t core.Transition) error {
	return a.retry(ctx, func(ctx context.Context) error {
		// A job has two states that are not final, so following the store twice is enough.
		for follows := 0; ; follows++ {
			_, err := st.Transition(ctx, t)
			if err == nil {
				return nil
			}
			job, jerr := st.Job(ctx, t.Job)
			switch {
			case jerr != nil:
				return err
			case job.State == t.To || job.State.Final():
				return nil
			case job.State != t.From && follows < 2:
				t.From = job.State
				continue
			case job.State != t.From:
				t.From = job.State
				return errMoved
			}
			if !transient(err) {
				return errMoved
			}
			return err
		}
	})
}

// launch starts the supervisor of a queued job and records the job as running. The order is the
// point: the supervisor is up and parked first, the store says running second, and the command
// starts third, when the machine asks to follow it. A crash before the second step leaves a queued
// job and a parked supervisor, which the next start discards; a crash after it leaves a running
// job whose supervisor the next start adopts and starts.
//
// Before all of it, it takes the leases of the resources the job holds alone that other daemons
// share. A lease held elsewhere ends the launch there, having started nothing. The leases travel
// with the launch's answer, and the loop keeps them while the machine holds the job.
func (a *App) launch(ctx context.Context, r lifecycle.LaunchRequest, scope core.Scope, known bool) {
	ref := r.Ref
	if !known {
		a.answer(ctx, lifecycle.LaunchFailed{Ref: ref, Cause: lifecycle.CauseRefused, Failure: *errNoScope, At: a.now()})
		return
	}
	held, refused := a.takeLeases(ctx, ref, r.Leases)
	if refused != nil {
		if ctx.Err() == nil {
			a.answer(ctx, *refused)
		}
		return
	}
	t := &taken{ref: ref, leases: held}
	h, gone, err := a.prepare(ctx, ref, r.Placement, r.Allocation, r.AllocationEnterBy, scope)
	posted := false
	switch {
	case ctx.Err() != nil:
		// The App is closing, so the machine hears nothing. A supervisor that Spawn started stays
		// for the next start to find, and Close gives up the connection to it: the App owns every
		// connection a call returned (ownedSupervisors), whether or not this goroutine hands it on.
	case gone:
		posted = a.postEffect(ctx, message{event: lifecycle.Closed{Ref: ref}, taken: t})
	case err != nil:
		posted = a.postEffect(ctx, message{event: lifecycle.LaunchFailed{Ref: ref, Cause: launchCauseOf(err), Failure: failureOf(err), At: a.now()}, handle: h, taken: t})
	default:
		posted = a.postEffect(ctx, message{event: lifecycle.Launched{Ref: ref}, handle: h, changed: true, taken: t})
	}
	if !posted {
		a.giveBack(held)
	}
}

// prepare does the steps of a launch up to and including recording the job as running, on the
// placement the scheduler started it on. gone says the job is not queued any more, so there is
// nothing to launch. allocation is the backend's reference for the standing allocation the job runs
// inside, empty for a job that is not in one, and enterBy the latest time it may enter it. A failure can leave a parked supervisor, which the handle reaches and the job's
// end releases.
func (a *App) prepare(ctx context.Context, ref lifecycle.Ref, placement, allocation string, enterBy time.Time, scope core.Scope) (h supervisor.Handle, gone bool, err error) {
	st, err := a.findStore(ctx, scope)
	if err != nil {
		return nil, false, storeError{err}
	}
	job, err := a.readJob(ctx, st, ref.Job)
	if err != nil {
		return nil, false, storeError{err}
	}
	if job.State != core.JobQueued {
		return nil, true, nil
	}
	spec := job.Spec
	if spec.Environment == core.EnvironmentAuto {
		// A submission decides the environment before it queues the job (keyFlow, placeFlow), so only
		// a command job queued by an older daemon has none. Guessing the host would run a task with a
		// Dockerfile outside its image, without the tools the image has, so the job is refused.
		return nil, false, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
			"job %s was queued without an environment, by an older daemon; submit it again to run it in the task's image, or with --env host to run it on the host", job.ID)}
	}
	if c, ok := spec.Settings.(core.CommandSettings); ok && c.Timeout <= 0 {
		c.Timeout = a.cfg.DefaultTimeout
		spec.Settings = c
	}
	// The job runs where the scheduler started it, one of the placements that give what its task
	// needs (schedule.PlacementDemand). The launch names it, so the supervisor decides nothing. A
	// daemon whose scheduler names no placement, as a test's may not, runs it on the one it was
	// restricted to.
	if placement != "" {
		spec.Placement = placement
	}
	env, err := a.cfg.Environments.Environment(spec.Environment, environment.Target{
		Repository: string(scope.Repository()), Commit: spec.Commit, Task: string(scope.Task()), Placement: spec.Placement,
	})
	if err != nil {
		return nil, false, err
	}
	fingerprint, err := env.Fingerprint(ctx)
	if err != nil {
		return nil, false, err
	}
	// An earlier attempt of a job queued again left output in the store, and this one's follows it,
	// after the line that says why it runs again.
	if err := ensureCutLine(ctx, st, job); err != nil {
		return nil, false, storeError{err}
	}
	base, err := logSize(ctx, st, job.ID)
	if err != nil {
		return nil, false, storeError{err}
	}
	launch := lifecycle.Launch{Repository: string(scope.Repository()), Task: string(scope.Task()), Job: job.ID, Spec: spec, OutputBase: base, Allocation: allocation, AllocationEnterBy: enterBy}
	if spec.Settings.Kind() != core.JobCommand {
		plan, err := a.planFlow(ctx, st, scope, job, fingerprint)
		if err != nil {
			return nil, false, err
		}
		launch.Plan = &plan
	}
	err = a.call(ctx, func(ctx context.Context) error {
		var err error
		h, err = a.cfg.Supervisors.Spawn(ctx, launch)
		return err
	})
	if err != nil {
		return nil, false, spawnError{err}
	}
	start := core.Transition{Job: job.ID, From: core.JobQueued, To: core.JobRunning, At: a.now(), Environment: &fingerprint}
	if launch.Plan != nil && launch.Plan.Key != job.Key {
		// It starts in another environment than it was keyed in, and runs under that one's key.
		start.Key = launch.Plan.Key
	}
	err = a.record(ctx, st, start)
	if err != nil {
		return h, false, storeError{err}
	}
	return h, false, nil
}

// follow lets the supervisor run the command if it has not yet, moves the job's output into its
// store as it appears, and tells the machine how the job ended. A failure is tried again from what
// the store holds, so no byte is lost or stored twice, and the attempts count from the last time
// output reached the store. What cannot be fixed that way ends in FollowFailed. A cancel goes first:
// it needs only the supervisor, and a store that is away must not hold it back.
func (a *App) follow(ctx context.Context, r lifecycle.Adopt, scope core.Scope, h supervisor.Handle) {
	if h == nil {
		a.answer(ctx, lifecycle.FollowFailed{Ref: r.Ref, Cause: lifecycle.CauseSupervisorSilent, Failure: *errNoHandle, At: a.now()})
		return
	}
	delay, attempts := a.cfg.Retry.First, 0
	cancelFirst := r.CancelFirst
	for {
		var err error
		var outcome lifecycle.Outcome
		progress := false
		if cancelFirst {
			if err = a.supervisorCall(ctx, h.Cancel, r.CancelReason); err == nil {
				cancelFirst = false
			}
		}
		if err == nil {
			var st store.Store
			if st, err = a.cfg.Stores.Find(ctx, scope); err != nil {
				err = storeError{err}
			} else {
				outcome, progress, err = a.followOnce(ctx, r.Ref.Job, st, h)
			}
		}
		g, gone := goneOf(err)
		switch {
		case ctx.Err() != nil:
			return
		case err == nil:
			a.answer(ctx, lifecycle.Finished{Ref: r.Ref, Outcome: outcome, At: a.now()})
			return
		case gone:
			a.answer(ctx, lifecycle.SupervisorLost{Ref: r.Ref, Proof: g.Proof, Start: g.Start, At: a.now()})
			return
		}
		a.cfg.Logf("job %s: following it: %v", r.Ref, err)
		if progress {
			delay, attempts = a.cfg.Retry.First, 0
		}
		attempts++
		if !transient(err) || attempts >= a.cfg.Retry.Attempts {
			a.answer(ctx, lifecycle.FollowFailed{Ref: r.Ref, Cause: causeOf(err), Failure: failureOf(err), At: a.now()})
			return
		}
		if !a.sleep(ctx, delay) {
			return
		}
		delay = min(delay*2, a.cfg.Retry.Longest)
	}
}

// supervisorCall makes one bounded call to a supervisor that takes a reason.
func (a *App) supervisorCall(ctx context.Context, do func(context.Context, string) error, reason string) error {
	ctx, done := clock.Deadline(ctx, a.cfg.Timers, a.cfg.CallTimeout)
	defer done()
	return do(ctx, reason)
}

// followOnce is one attempt to follow a job from what the store holds.
func (a *App) followOnce(ctx context.Context, job core.JobID, st store.Store, h supervisor.Handle) (lifecycle.Outcome, bool, error) {
	start, done := clock.Deadline(ctx, a.cfg.Timers, a.cfg.CallTimeout)
	err := h.Start(start)
	done()
	if err != nil {
		return lifecycle.Outcome{}, false, err
	}
	offset, err := logSize(ctx, st, job)
	if err != nil {
		return lifecycle.Outcome{}, false, storeError{err}
	}
	return a.pump(ctx, job, st, h, offset)
}

// pump passes a job's output to its store from the offset and returns its outcome. Output is
// written in batches, when a batch is full and when it has waited long enough, so a job that
// prints steadily does not make a write per line; the last batch is written before the outcome.
// progress says output reached the store.
func (a *App) pump(ctx context.Context, job core.JobID, st store.Store, h supervisor.Handle, offset uint64) (o lifecycle.Outcome, progress bool, err error) {
	ctx, cancel := context.WithCancel(ctx)
	var follower owned.Group
	defer follower.Wait()
	defer cancel()

	chunks := make(chan []byte)
	ended := make(chan struct{})
	var outcome lifecycle.Outcome
	var followErr error
	follower.Go(func() {
		defer close(ended)
		outcome, followErr = h.Follow(ctx, offset, func(data []byte) error {
			select {
			case chunks <- bytes.Clone(data):
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		})
	})

	var batch []byte
	var due <-chan struct{}
	stopTimer := func() {}
	defer func() { stopTimer() }()
	flush := func() error {
		stopTimer()
		stopTimer, due = func() {}, nil
		if len(batch) == 0 {
			return nil
		}
		if err := st.AppendLog(ctx, job, batch); err != nil {
			return storeError{err}
		}
		batch, progress = nil, true
		a.logsChanged.Notify()
		return nil
	}
	for {
		select {
		case data := <-chunks:
			batch = append(batch, data...)
			switch {
			case len(batch) >= a.cfg.LogBatchBytes:
				if err := flush(); err != nil {
					return lifecycle.Outcome{}, progress, err
				}
			case due == nil:
				due, stopTimer = a.cfg.Timers.Timeout(ctx, a.cfg.LogBatchDelay)
			}
		case <-due:
			if err := flush(); err != nil {
				return lifecycle.Outcome{}, progress, err
			}
		case <-ended:
			if err := flush(); err != nil {
				return lifecycle.Outcome{}, progress, err
			}
			return outcome, progress, followErr
		}
	}
}

// signalCancel passes a cancel on to a supervisor. One that did not arrive is offered again by the
// machine; the job's outcome is still what ends it, and a supervisor that is gone is reported by
// following it.
func (a *App) signalCancel(ctx context.Context, r lifecycle.SignalCancel, h supervisor.Handle) {
	err := error(errNoHandle)
	if h != nil {
		err = a.call(ctx, func(ctx context.Context) error { return h.Cancel(ctx, r.Reason) })
	}
	if err != nil && ctx.Err() == nil {
		a.cfg.Logf("job %s: asking its supervisor to cancel: %v", r.Ref, err)
		a.answer(ctx, lifecycle.CancelFailed{Ref: r.Ref})
	}
}

// pastTheEnd is an offset beyond any output, for following a job only to learn its outcome.
const pastTheEnd = 1 << 62

// stopJob ends the command of a job the daemon gave up following and learns how it ended.
func (a *App) stopJob(ctx context.Context, r lifecycle.Stop, h supervisor.Handle) {
	if h == nil {
		a.answer(ctx, lifecycle.StopFailed{Ref: r.Ref, Failure: *errNoHandle})
		return
	}
	var outcome lifecycle.Outcome
	err := a.call(ctx, func(ctx context.Context) error { return h.Cancel(ctx, r.Reason) })
	if err == nil {
		err = a.retry(ctx, func(ctx context.Context) error {
			ctx, done := clock.Deadline(ctx, a.cfg.Timers, a.cfg.StopTimeout)
			defer done()
			var err error
			outcome, err = h.Follow(ctx, pastTheEnd, func([]byte) error { return nil })
			return err
		})
	}
	g, gone := goneOf(err)
	switch {
	case ctx.Err() != nil:
	case err == nil:
		a.answer(ctx, lifecycle.Finished{Ref: r.Ref, Outcome: outcome, At: a.now()})
	case gone:
		a.answer(ctx, lifecycle.SupervisorLost{Ref: r.Ref, Proof: g.Proof, Start: g.Start, At: a.now()})
	default:
		a.answer(ctx, lifecycle.StopFailed{Ref: r.Ref, Failure: failureOf(err)})
	}
}

// move records the end of a job, or the cut-off attempt of one queued again: its explanation in the
// output first, so that a reader who sees the job final sees the line, then the transition, then
// the release of the supervisor. The job
// stays in the machine until the end is recorded. The supervisor is kept until then too, since it
// holds the outcome. A release that fails after that loses nothing: the next start clears the run.
func (a *App) move(ctx context.Context, m lifecycle.Move, scope core.Scope, known bool, h supervisor.Handle) {
	err := error(errNoScope)
	if known {
		err = a.moveInStore(ctx, m, scope)
	}
	switch {
	case ctx.Err() != nil:
		return
	case err != nil:
		a.cfg.Logf("job %s: recording its end: %v", m.Ref, err)
		a.answer(ctx, lifecycle.MoveFailed{Ref: m.Ref, Failure: failureOf(err)})
		return
	}
	a.notifyJobs()
	if m.Settlement.Requeues() {
		// The machine releases the run that was cut off before it launches the job again.
		a.answer(ctx, lifecycle.Requeued{Ref: m.Ref})
		return
	}
	if m.Release {
		if err := a.release(ctx, m.Ref, h); err != nil {
			a.cfg.Logf("job %s: releasing its supervisor: %v", m.Ref, err)
		}
	}
	a.answer(ctx, lifecycle.Closed{Ref: m.Ref})
}

func (a *App) moveInStore(ctx context.Context, m lifecycle.Move, scope core.Scope) error {
	st, err := a.findStore(ctx, scope)
	if err != nil {
		return err
	}
	if m.Settlement.Requeues() {
		return a.requeueInStore(ctx, st, m)
	}
	if m.Settlement.Log == "" && m.Settlement.Transition.From != core.JobQueued {
		return a.record(ctx, st, m.Settlement.Transition)
	}
	job, err := a.readJob(ctx, st, m.Ref.Job)
	if err != nil {
		return err
	}
	if job.State.Final() {
		// An end that is recorded already has its own explanation, which a line after it would
		// contradict: the job was not in the daemon's hands when this one was decided.
		return nil
	}
	err = a.retry(ctx, func(ctx context.Context) error {
		if job.State == core.JobQueued {
			// A job that ends from the queue says first why it was there again.
			if err := ensureCutLine(ctx, st, job); err != nil {
				return err
			}
		}
		if m.Settlement.Log == "" {
			return nil
		}
		return appendLineOnce(ctx, st, m.Ref.Job, m.Settlement.Log)
	})
	if err != nil {
		return fmt.Errorf("adding a line to its output: %w", err)
	}
	return a.record(ctx, st, m.Settlement.Transition)
}

// requeueInStore queues a job again and then says why in its output. The line promises the job runs
// again, so it follows the transition: written first, a crash before the transition could leave it
// in the output of a job that a later decision ends. A line lost to a crash after the transition is
// written by the job's next launch (see ensureCutLine).
func (a *App) requeueInStore(ctx context.Context, st store.Store, m lifecycle.Move) error {
	if err := a.record(ctx, st, m.Settlement.Transition); err != nil {
		return err
	}
	job, err := a.readJob(ctx, st, m.Ref.Job)
	if err == nil && job.State == core.JobQueued {
		err = a.retry(ctx, func(ctx context.Context) error { return ensureCutLine(ctx, st, job) })
	}
	if err != nil {
		a.cfg.Logf("job %s: saying why it runs again: %v", m.Ref, err)
	}
	return nil
}

// ensureCutLine writes the line of a queued job's last attempt that was cut off, unless its output
// ends with it already.
func ensureCutLine(ctx context.Context, st store.Store, job core.Job) error {
	n := len(job.Attempts)
	if n == 0 {
		return nil
	}
	return appendLineOnce(ctx, st, job.ID, lifecycle.CutLine(n, job.Attempts[n-1].Cut))
}

// discard clears what is left of a run, when no job needs it.
func (a *App) discard(ctx context.Context, d lifecycle.Discard, h supervisor.Handle) {
	err := a.release(ctx, d.Ref, h)
	switch {
	case ctx.Err() != nil:
	case err != nil:
		a.cfg.Logf("job %s: clearing its run: %v", d.Ref, err)
		a.answer(ctx, lifecycle.DiscardFailed{Ref: d.Ref, Failure: failureOf(err)})
	case d.Queued:
		a.answer(ctx, lifecycle.Discarded{Ref: d.Ref, At: a.now()})
	default:
		a.answer(ctx, lifecycle.Closed{Ref: d.Ref})
	}
}

// release lets go of a job's supervisor and removes what is left of its run. It is for a job whose
// end is recorded or that never ran. It goes on while the daemon stops, as it is the last step of
// something already decided, and its calls are bounded so that it cannot hold the stop. With no
// handle it looks at what is there, and only a proof of death lets a run go.
func (a *App) release(ctx context.Context, ref lifecycle.Ref, h supervisor.Handle) error {
	ctx = context.WithoutCancel(ctx)
	if h == nil {
		run, err := a.probe(ctx, ref)
		if err != nil {
			return err
		}
		switch r := run.(type) {
		case supervisor.None:
			return nil
		case supervisor.Dead:
			return a.forget(ctx, ref, r.Proof)
		case supervisor.Unknown:
			return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("job %s: what is left of its supervisor may still live", ref)}
		case supervisor.Alive:
			h = r.Handle
			defer h.Close()
		}
	}
	proof := lifecycle.ProofReleased
	if err := a.call(ctx, h.Release); err != nil {
		p, gone := goneProof(err)
		if !gone {
			return err
		}
		proof = p
	}
	return a.forget(ctx, ref, proof)
}

func (a *App) forget(ctx context.Context, ref lifecycle.Ref, proof lifecycle.DeathProof) error {
	return a.call(ctx, func(ctx context.Context) error { return a.cfg.Supervisors.Forget(ctx, ref, proof) })
}

// armRetry answers after the slow period.
func (a *App) armRetry(ctx context.Context, ref lifecycle.Ref) {
	if a.sleep(ctx, a.cfg.SlowPeriod) {
		a.answer(ctx, lifecycle.RetryDue{Ref: ref})
	}
}

// reexamine looks again at a job in doubt.
func (a *App) reexamine(ctx context.Context, ref lifecycle.Ref, scope core.Scope, known bool) {
	run := a.look(ctx, ref)
	if !known {
		scope, known = scopeOfRun(run)
	}
	found := lifecycle.Recovered{Ref: ref, Finding: lifecycle.StoreAway{}, Supervisor: a.presenceOf(run)}
	m := message{handle: handleOf(run)}
	if known {
		found.Finding = a.lookInStore(ctx, ref, scope, found.Supervisor)
		m.scope = &scope
	}
	found.At = a.now()
	m.event = found
	a.postEffect(ctx, m)
}

// look finds what is left of a job's run. A look that fails says nothing, which is what Unknown
// says.
func (a *App) look(ctx context.Context, ref lifecycle.Ref) supervisor.Run {
	run, err := a.probe(ctx, ref)
	if err != nil {
		a.cfg.Logf("job %s: looking for its supervisor: %v", ref, err)
		return supervisor.Unknown{Ref: ref}
	}
	return run
}

func (a *App) probe(ctx context.Context, ref lifecycle.Ref) (supervisor.Run, error) {
	ctx, done := clock.Deadline(ctx, a.cfg.Timers, a.cfg.CallTimeout)
	defer done()
	return a.cfg.Supervisors.Probe(ctx, ref)
}

// presenceOf says what a run means to the machine. A run that does not say its limit has the
// default one: every launch has a limit, and only the record of it can be lost.
func (a *App) presenceOf(run supervisor.Run) lifecycle.Presence {
	switch p := run.Found().(type) {
	case lifecycle.RunAlive:
		if p.Limit <= 0 {
			p.Limit = a.cfg.DefaultTimeout
		}
		return p
	case lifecycle.RunUnknown:
		if p.Limit <= 0 {
			p.Limit = a.cfg.DefaultTimeout
		}
		return p
	case lifecycle.RunDead, lifecycle.RunNone:
		return p
	}
	return lifecycle.RunUnknown{Limit: a.cfg.DefaultTimeout}
}

// handleOf is the handle of a run, and nil for one with none.
func handleOf(run supervisor.Run) supervisor.Handle {
	if alive, ok := run.(supervisor.Alive); ok {
		return alive.Handle
	}
	return nil
}

// lookInStore says what a store has of a job, whose run was found as presence. It makes nothing: a
// store that is not there is away, as this is only asked of a job that had one.
func (a *App) lookInStore(ctx context.Context, ref lifecycle.Ref, scope core.Scope, presence lifecycle.Presence) lifecycle.Finding {
	st, err := a.cfg.Stores.Find(ctx, scope)
	if err != nil {
		return lifecycle.StoreAway{}
	}
	job, err := st.Job(ctx, ref.Job)
	var f *core.Failure
	switch {
	case err == nil:
		var given []core.JobID
		if job.State == core.JobRunning {
			// A store that cannot say what it gave up gives up nothing, which holds the job in doubt.
			if given, err = a.cfg.Stores.GivenUp(ctx, scope); err != nil {
				a.cfg.Logf("job %s: reading which jobs its store gave up: %v", ref, err)
			}
		}
		return a.jobIn(ctx, scope, job, presence, given)
	case errors.As(err, &f) && f.Kind == core.FailureNotFound && !errors.Is(err, store.ErrAway):
		return lifecycle.NoSuchJob{}
	}
	return lifecycle.StoreAway{}
}

// jobIn is what a store has of a job, whose run was found as presence, given the jobs the store gave
// up. A running job the user gave up is given up only when no run of it is found here: a run found
// here is this daemon's, so the job is taken back first.
func (a *App) jobIn(ctx context.Context, scope core.Scope, job core.Job, presence lifecycle.Presence, given []core.JobID) lifecycle.JobIn {
	in := lifecycle.JobIn{
		State: job.State, SubmittedAt: job.SubmittedAt, Attempts: len(job.Attempts), Policy: core.RerunPolicyOf(job.Spec.Settings.Kind()),
		Work: a.workOf(job),
	}
	if job.State != core.JobRunning || !slices.Contains(given, job.ID) {
		return in
	}
	if _, here := presence.(lifecycle.RunAlive); here {
		if err := a.retry(ctx, func(ctx context.Context) error { return a.cfg.Stores.Reclaim(ctx, scope, job.ID) }); err != nil {
			a.cfg.Logf("job %s: a run of it is here, and its store still has it given up: %v", job.ID, err)
		}
		return in
	}
	in.GivenUp = true
	return in
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
