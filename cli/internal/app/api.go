package app

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"unicode/utf8"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// SubmitRequest is a submission as the daemon receives it.
type SubmitRequest struct {
	Context core.CallContext
	Spec    core.JobSpec
	NoCache bool
	Note    string
	Plan    core.PlanNumber
}

func invalid(format string, args ...any) *core.Failure {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(format, args...)}
}

// Submit queues a job in the store of its task and makes the machine know of it. It returns once
// the job is durable; the job runs later, in its turn.
//
// A check or a measure job is keyed first: the task is read as the submission's commit has it,
// the environment is decided and fingerprinted, and the job's cache key is made (keyFlow). A
// command job has no key.
func (a *App) Submit(ctx context.Context, req SubmitRequest) (core.SubmitOutcome, error) {
	if req.Spec.Settings == nil {
		return core.SubmitOutcome{}, invalid("spec: no job settings")
	}
	if !req.Spec.Commit.Valid() {
		return core.SubmitOutcome{}, invalid("spec.commit: %q is not a full 40-digit commit id", req.Spec.Commit)
	}
	if len(req.Note) > a.cfg.MaxNoteBytes {
		return core.SubmitOutcome{}, invalid("note: %d bytes is over the limit of %d", len(req.Note), a.cfg.MaxNoteBytes)
	}
	if !utf8.ValidString(req.Note) {
		return core.SubmitOutcome{}, invalid("note: not valid UTF-8")
	}
	scope := req.Context.Scope
	if err := resolved(scope); err != nil {
		return core.SubmitOutcome{}, err
	}
	// A submission that cannot be keyed, such as one for a task its commit does not have, makes no
	// store. Only a submission that can be queued does.
	spec, key := req.Spec, core.JobKey("")
	if spec.Settings.Kind() != core.JobCommand {
		var err error
		if spec, key, err = a.keyFlow(ctx, req.Context, spec); err != nil {
			return core.SubmitOutcome{}, err
		}
	}
	ctx = a.generationContext(ctx, storeIDOf(scope))
	st, err := a.createStore(ctx, scope)
	if err != nil {
		return core.SubmitOutcome{}, err
	}
	at := a.now()
	sub := core.Submission{Context: req.Context, Spec: spec, Key: key, NoCache: req.NoCache, Plan: req.Plan, At: at}
	if req.Note != "" {
		sub.Note = &core.Note{Text: req.Note, At: at, Agent: req.Context.Agent}
	}

	out, err := st.Submit(ctx, sub)
	if err != nil {
		// The change may have been made with only its answer lost. The sweep finds such a job.
		return core.SubmitOutcome{}, err
	}
	// A successful write supersedes older observations, but it cannot clear an
	// unread store until all earlier unfinished jobs have been recovered.
	readiness := storeReadable
	var recoveryErr error
	if slices.Contains(a.view.Load().unread, scope) {
		recoveryErr = a.readAgain(ctx, scope)
		if recoveryErr != nil {
			readiness = storeRecoveryPending
		}
	}
	var event lifecycle.Event = lifecycle.StoreRead{Store: storeIDOf(scope)}
	if recoveryErr != nil {
		event = lifecycle.StoreUnread{Store: storeIDOf(scope), Failure: failureOf(recoveryErr)}
	}
	if out.Disposition != core.SubmitReused && out.Job.State == core.JobQueued {
		event = admittedOf(lifecycle.Ref{Store: storeIDOf(scope), Job: out.Job.ID}, out.Job)
	}
	// Settled, so a returned submission is in Status, even while recovery waits.
	err = a.settleEffect(ctx, message{event: event, readiness: readiness, scope: &scope, changed: true})
	// The store has accepted the job. Closing only prevents this App from
	// observing it; the next App recovers it. Do not invite a duplicate retry.
	if errors.Is(err, context.Canceled) && a.ctx.Err() != nil {
		return out, nil
	}
	return out, err
}

// createStore returns the store of a valid scope, making it if needed, trying for a bounded
// time. Only the calls that write a task's first record make a store: a submission and a new plan.
func (a *App) createStore(ctx context.Context, scope core.Scope) (store.Store, error) {
	var st store.Store
	err := a.retry(ctx, func(ctx context.Context) error {
		var err error
		st, err = a.cfg.Stores.Create(ctx, scope)
		return err
	})
	return st, err
}

// open returns the store of the scope, or says what is wrong with the scope. It makes nothing.
func (a *App) open(ctx context.Context, scope core.Scope) (store.Store, error) {
	if err := resolved(scope); err != nil {
		return nil, err
	}
	return a.findStore(ctx, scope)
}

// Job returns one job.
func (a *App) Job(ctx context.Context, scope core.Scope, id core.JobID) (core.Job, error) {
	st, err := a.open(ctx, scope)
	if err != nil {
		return core.Job{}, err
	}
	return a.readJob(ctx, st, id)
}

// Jobs returns the jobs of a task that match the filter, oldest first. A task with no store has
// none.
func (a *App) Jobs(ctx context.Context, scope core.Scope, filter core.JobFilter) ([]core.Job, error) {
	st, err := a.open(ctx, scope)
	if errors.Is(err, store.ErrNoStore) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var jobs []core.Job
	err = a.retry(ctx, func(ctx context.Context) error {
		var err error
		jobs, err = st.Jobs(ctx, filter)
		return err
	})
	return jobs, err
}

// AddNote appends a note to a job in any state.
func (a *App) AddNote(ctx context.Context, c core.CallContext, id core.JobID, text string) (core.Job, error) {
	if text == "" {
		return core.Job{}, invalid("text: a note is not empty")
	}
	if len(text) > a.cfg.MaxNoteBytes {
		return core.Job{}, invalid("text: %d bytes is over the limit of %d", len(text), a.cfg.MaxNoteBytes)
	}
	if !utf8.ValidString(text) {
		return core.Job{}, invalid("text: not valid UTF-8")
	}
	st, err := a.open(ctx, c.Scope)
	if err != nil {
		return core.Job{}, err
	}
	job, err := st.AddNote(ctx, id, core.Note{Text: text, At: a.now(), Agent: c.Agent})
	if err != nil {
		return core.Job{}, err
	}
	a.notifyJobs()
	return job, nil
}

// Cancel ends a job and returns it once it is final: a queued job is cancelled at once, and a
// running job's whole process group is ended first. A job that is final already is returned as it
// is. Every caller who cancels, or waits for, a job gets the same end. A job whose end is out of
// the daemon's hands for now fails the call with why; the cancel stays with the machine.
//
// The request is made again on every change and every slow period, so one that reached the machine
// before it heard of the job is not lost. The machine counts a job's cancel once.
func (a *App) Cancel(ctx context.Context, c core.CallContext, id core.JobID, reason string) (core.Job, error) {
	scope := c.Scope
	ctx = a.generationContext(ctx, storeIDOf(scope))
	st, err := a.open(ctx, scope)
	if err != nil {
		return core.Job{}, err
	}
	ref := lifecycle.Ref{Store: storeIDOf(scope), Job: id}
	for {
		wake := a.jobsChanged.Wait()
		job, err := a.readJob(ctx, st, id)
		if err != nil || job.State.Final() {
			return job, err
		}
		a.postEffect(ctx, message{event: lifecycle.CancelAsked{
			Ref: ref, Reason: reason, State: job.State, SubmittedAt: job.SubmittedAt, At: a.now(),
		}, scope: &scope})
		if f, troubled := a.troubleOf(ref); troubled {
			return core.Job{}, &f
		}
		if err := a.pause(ctx, wake); err != nil {
			return core.Job{}, err
		}
	}
}

// pause waits for a wake-up, for the slow period, or for ctx.
func (a *App) pause(ctx context.Context, wake <-chan struct{}) error {
	_, err := a.pauseUntil(ctx, wake, nil)
	return err
}

// pauseUntil is pause that also returns, with true, when deadline is closed.
func (a *App) pauseUntil(ctx context.Context, wake, deadline <-chan struct{}) (bool, error) {
	fired, stop := a.cfg.Timers.Timeout(ctx, a.cfg.SlowPeriod)
	defer stop()
	select {
	case <-wake:
		return false, nil
	case <-fired:
		return false, nil
	case <-deadline:
		return true, nil
	case <-ctx.Done():
		return false, ctx.Err()
	}
}

// Watch passes the current state of each job to emit, and then each change, until every job is
// final or ctx is done. A job is passed again only when something about it changed. A job whose end
// is out of the daemon's hands for now, or that waits on such a job or on its store, ends the watch
// with why, so a waiter is never left waiting for an end nobody is working on, and never told one
// that did not happen.
func (a *App) Watch(ctx context.Context, scope core.Scope, ids []core.JobID, emit func(core.Job) error) error {
	_, err := a.Wait(ctx, scope, core.WaitFor{IDs: ids}, emit)
	return err
}

// Wait passes the jobs' states to emit as Watch does, and returns how the wait ended once
// core.DecideWait says it is over, or once the timeout passed. The daemon decides the outcome, so
// every waiter on the same jobs reads the same one. A job whose end is out of the daemon's hands
// for now ends the wait with why, as for Watch.
func (a *App) Wait(ctx context.Context, scope core.Scope, req core.WaitFor, emit func(core.Job) error) (core.WaitOutcome, error) {
	if len(req.IDs) == 0 {
		return core.WaitOutcome{}, invalid("job_ids: name at least one job to wait for")
	}
	if req.Timeout < 0 {
		return core.WaitOutcome{}, invalid("timeout: %v is negative", req.Timeout)
	}
	st, err := a.open(ctx, scope)
	if err != nil {
		return core.WaitOutcome{}, err
	}
	var deadline <-chan struct{}
	if req.Timeout > 0 {
		fired, stop := a.cfg.Timers.Timeout(ctx, req.Timeout)
		defer stop()
		deadline = fired
	}
	last := map[core.JobID]core.Job{}
	for {
		// The wake-up is taken before the look, so a change in between wakes the next wait.
		wake := a.jobsChanged.Wait()
		jobs := make([]core.Job, 0, len(req.IDs))
		for _, id := range req.IDs {
			job, err := a.readJob(ctx, st, id)
			if err != nil {
				return core.WaitOutcome{}, err
			}
			if prev, seen := last[id]; !seen || !reflect.DeepEqual(prev, job) {
				if err := emit(job); err != nil {
					return core.WaitOutcome{}, err
				}
				last[id] = job
			}
			jobs = append(jobs, job)
		}
		if out, over := core.DecideWait(jobs, req.Any); over {
			return out, nil
		}
		for _, job := range jobs {
			if job.State.Final() {
				continue
			}
			if f, waits := a.waitOf(lifecycle.Ref{Store: storeIDOf(scope), Job: job.ID}); waits {
				return core.WaitOutcome{}, &f
			}
		}
		timedOut, err := a.pauseUntil(ctx, wake, deadline)
		if err != nil {
			return core.WaitOutcome{}, err
		}
		if timedOut {
			return core.TimedOutWait(jobs), nil
		}
	}
}

// ReadLogs passes a job's output from the offset to emit, and with follow keeps passing it as it
// grows until the job is final and every byte was passed. Without follow it passes what is there
// and returns.
func (a *App) ReadLogs(ctx context.Context, scope core.Scope, id core.JobID, offset uint64, follow bool, emit func(core.LogChunk) error) error {
	st, err := a.open(ctx, scope)
	if err != nil {
		return err
	}
	for {
		wake := a.logsChanged.Wait()
		// The state is read before the output: a job that is final has written all of its
		// output, so reading to the end after seeing it final passes everything.
		job, err := a.readJob(ctx, st, id)
		if err != nil {
			return err
		}
		for {
			var chunk core.LogChunk
			err := a.retry(ctx, func(ctx context.Context) error {
				var err error
				chunk, err = st.ReadLog(ctx, id, offset, logChunk)
				return err
			})
			if err != nil {
				return err
			}
			if len(chunk.Data) == 0 {
				break
			}
			if err := emit(chunk); err != nil {
				return err
			}
			offset = chunk.Next
		}
		if !follow || job.State.Final() {
			return nil
		}
		if f, waits := a.waitOf(lifecycle.Ref{Store: storeIDOf(scope), Job: id}); waits {
			return &f
		}
		if err := a.pause(ctx, wake); err != nil {
			return err
		}
	}
}
