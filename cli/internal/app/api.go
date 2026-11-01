package app

import (
	"context"
	"fmt"
	"reflect"
	"unicode/utf8"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
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
	t, err := a.open(ctx, scopeOf(req.Context))
	if err != nil {
		return core.SubmitOutcome{}, err
	}
	at := a.now()
	sub := core.Submission{Context: req.Context, Spec: req.Spec, NoCache: req.NoCache, Plan: req.Plan, At: at}
	if req.Note != "" {
		sub.Note = &core.Note{Text: req.Note, At: at, Agent: req.Context.Agent}
	}

	a.admit.Lock()
	defer a.admit.Unlock()
	out, err := t.store.Submit(ctx, sub)
	if err != nil {
		return core.SubmitOutcome{}, err
	}
	if out.Disposition != core.SubmitReused && out.Job.State == core.JobQueued {
		a.dispatch(lifecycle.Admitted{
			Ref:         lifecycle.Ref{Store: lifecycle.StoreIDOf(t.scope.Repository, t.scope.Task), Job: out.Job.ID},
			SubmittedAt: out.Job.SubmittedAt,
		})
	}
	a.notifyJobs()
	return out, nil
}

// Job returns one job.
func (a *App) Job(ctx context.Context, scope Scope, id core.JobID) (core.Job, error) {
	t, err := a.open(ctx, scope)
	if err != nil {
		return core.Job{}, err
	}
	return t.store.Job(ctx, id)
}

// Jobs returns the jobs of a task that match the filter, oldest first.
func (a *App) Jobs(ctx context.Context, scope Scope, filter core.JobFilter) ([]core.Job, error) {
	t, err := a.open(ctx, scope)
	if err != nil {
		return nil, err
	}
	return t.store.Jobs(ctx, filter)
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
	t, err := a.open(ctx, scopeOf(c))
	if err != nil {
		return core.Job{}, err
	}
	job, err := t.store.AddNote(ctx, id, core.Note{Text: text, At: a.now(), Agent: c.Agent})
	if err != nil {
		return core.Job{}, err
	}
	a.notifyJobs()
	return job, nil
}

// Cancel ends a job and returns it once it is final: a queued job is cancelled at once, and a
// running job's whole process group is ended first. A job that is final already is returned as it
// is. Every caller who cancels, or waits for, a job gets the same end.
func (a *App) Cancel(ctx context.Context, c core.CallContext, id core.JobID, reason string) (core.Job, error) {
	t, err := a.open(ctx, scopeOf(c))
	if err != nil {
		return core.Job{}, err
	}
	ref := lifecycle.Ref{Store: lifecycle.StoreIDOf(t.scope.Repository, t.scope.Task), Job: id}

	// Under the admit lock the machine knows of every job the store has queued, so the request
	// below reaches it.
	a.admit.Lock()
	job, err := t.store.Job(ctx, id)
	if err != nil || job.State.Final() {
		a.admit.Unlock()
		return job, err
	}
	a.dispatch(lifecycle.CancelAsked{Ref: ref, Reason: reason, At: a.now()})
	a.admit.Unlock()

	for {
		wake := a.jobsChanged.Wait()
		job, err := t.store.Job(ctx, id)
		if err != nil || job.State.Final() {
			return job, err
		}
		select {
		case <-wake:
		case <-ctx.Done():
			return core.Job{}, ctx.Err()
		}
	}
}

// Watch passes the current state of each job to emit, and then each change, until every job is
// final or ctx is done. A job is passed again only when something about it changed.
func (a *App) Watch(ctx context.Context, scope Scope, ids []core.JobID, emit func(core.Job) error) error {
	if len(ids) == 0 {
		return invalid("job_ids: name at least one job to watch")
	}
	t, err := a.open(ctx, scope)
	if err != nil {
		return err
	}
	last := map[core.JobID]core.Job{}
	for {
		// The wake-up is taken before the look, so a change in between wakes the next wait.
		wake := a.jobsChanged.Wait()
		final := 0
		for _, id := range ids {
			job, err := t.store.Job(ctx, id)
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
			}
		}
		if final == len(ids) {
			return nil
		}
		select {
		case <-wake:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// ReadLogs passes a job's output from the offset to emit, and with follow keeps passing it as it
// grows until the job is final and every byte was passed. Without follow it passes what is there
// and returns.
func (a *App) ReadLogs(ctx context.Context, scope Scope, id core.JobID, offset uint64, follow bool, emit func(core.LogChunk) error) error {
	t, err := a.open(ctx, scope)
	if err != nil {
		return err
	}
	for {
		wake := a.logsChanged.Wait()
		// The state is read before the output: a job that is final has written all of its
		// output, so reading to the end after seeing it final passes everything.
		job, err := t.store.Job(ctx, id)
		if err != nil {
			return err
		}
		for {
			chunk, err := t.store.ReadLog(ctx, id, offset, logChunk)
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
		select {
		case <-wake:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}
