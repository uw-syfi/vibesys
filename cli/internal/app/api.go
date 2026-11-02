package app

import (
	"context"
	"fmt"
	"reflect"
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

func scopeOf(c core.CallContext) Scope { return Scope{Repository: c.Repository, Task: c.Task} }

func invalid(format string, args ...any) *core.Failure {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(format, args...)}
}

// Submit queues a job in the store of its task and makes the machine know of it. It returns once
// the job is durable; the job runs later, in its turn.
//
// This build runs command jobs. A check or a measure job needs an environment that can make a
// clean checkout and read a task's files, which come with the real environments, and is refused
// as unavailable and not queued to fail later.
func (a *App) Submit(ctx context.Context, req SubmitRequest) (core.SubmitOutcome, error) {
	if req.Spec.Settings == nil {
		return core.SubmitOutcome{}, invalid("spec: no job settings")
	}
	if kind := req.Spec.Settings.Kind(); kind != core.JobCommand {
		return core.SubmitOutcome{}, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
			"this build runs command jobs only; %s jobs need the host or docker environment", kind)}
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
	scope := scopeOf(req.Context)
	st, err := a.open(ctx, scope)
	if err != nil {
		return core.SubmitOutcome{}, err
	}
	at := a.now()
	sub := core.Submission{Context: req.Context, Spec: req.Spec, NoCache: req.NoCache, Plan: req.Plan, At: at}
	if req.Note != "" {
		sub.Note = &core.Note{Text: req.Note, At: at, Agent: req.Context.Agent}
	}

	out, err := st.Submit(ctx, sub)
	if err != nil {
		// The change may have been made with only its answer lost. The sweep finds such a job.
		return core.SubmitOutcome{}, err
	}
	if out.Disposition != core.SubmitReused && out.Job.State == core.JobQueued {
		a.post(message{
			event: lifecycle.Admitted{Ref: lifecycle.Ref{Store: storeIDOf(scope), Job: out.Job.ID}, SubmittedAt: out.Job.SubmittedAt},
			scope: &scope,
		})
	}
	a.notifyJobs()
	return out, nil
}

// open returns the store of the scope, or says what is wrong with the scope.
func (a *App) open(ctx context.Context, scope Scope) (store.Store, error) {
	if err := scope.Validate(); err != nil {
		return nil, err
	}
	return a.openStore(ctx, scope)
}

// Job returns one job.
func (a *App) Job(ctx context.Context, scope Scope, id core.JobID) (core.Job, error) {
	st, err := a.open(ctx, scope)
	if err != nil {
		return core.Job{}, err
	}
	return a.readJob(ctx, st, id)
}

// Jobs returns the jobs of a task that match the filter, oldest first.
func (a *App) Jobs(ctx context.Context, scope Scope, filter core.JobFilter) ([]core.Job, error) {
	st, err := a.open(ctx, scope)
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
	st, err := a.open(ctx, scopeOf(c))
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
	scope := scopeOf(c)
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
		a.post(message{event: lifecycle.CancelAsked{
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
	fired, stop := a.cfg.Timers.Timeout(ctx, a.cfg.SlowPeriod)
	defer stop()
	select {
	case <-wake:
		return nil
	case <-fired:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Watch passes the current state of each job to emit, and then each change, until every job is
// final or ctx is done. A job is passed again only when something about it changed. A job whose end
// is out of the daemon's hands for now ends the watch with why, so a waiter is never left waiting
// for an end nobody is working on, and never told one that did not happen.
func (a *App) Watch(ctx context.Context, scope Scope, ids []core.JobID, emit func(core.Job) error) error {
	if len(ids) == 0 {
		return invalid("job_ids: name at least one job to watch")
	}
	st, err := a.open(ctx, scope)
	if err != nil {
		return err
	}
	last := map[core.JobID]core.Job{}
	for {
		// The wake-up is taken before the look, so a change in between wakes the next wait.
		wake := a.jobsChanged.Wait()
		final := 0
		for _, id := range ids {
			job, err := a.readJob(ctx, st, id)
			if err != nil {
				return err
			}
			if prev, seen := last[id]; !seen || !reflect.DeepEqual(prev, job) {
				if err := emit(job); err != nil {
					return err
				}
				last[id] = job
			}
			if job.State.Final() {
				final++
			} else if f, troubled := a.troubleOf(lifecycle.Ref{Store: storeIDOf(scope), Job: id}); troubled {
				return &f
			}
		}
		if final == len(ids) {
			return nil
		}
		if err := a.pause(ctx, wake); err != nil {
			return err
		}
	}
}

// ReadLogs passes a job's output from the offset to emit, and with follow keeps passing it as it
// grows until the job is final and every byte was passed. Without follow it passes what is there
// and returns.
func (a *App) ReadLogs(ctx context.Context, scope Scope, id core.JobID, offset uint64, follow bool, emit func(core.LogChunk) error) error {
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
		if f, troubled := a.troubleOf(lifecycle.Ref{Store: storeIDOf(scope), Job: id}); troubled {
			return &f
		}
		if err := a.pause(ctx, wake); err != nil {
			return err
		}
	}
}
