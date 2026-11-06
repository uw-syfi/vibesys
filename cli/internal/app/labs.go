package app

import (
	"context"
	"errors"
	"fmt"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// Lab makes one call on a lab job (core.LabSpec) and passes the replies to emit, the answer last.
// The lab itself is its supervisor's, which outlives the daemon, so a lab that ran before a restart
// of the daemon is reached as it was.
//
// A call waits while the lab is queued, and its supervisor waits while it is starting; a status
// answers at once, and an end of a queued lab cancels it. A queued lab that is held, or one the
// daemon will not start while it drains, keeps its place in the queue, and a call that would wait
// for it is core.FailureConflict, saying who steered it back and how it starts, rather than waiting
// for an operator. A call on a job that is not a lab is core.FailureInvalidInput, and one on a lab
// that ended is core.FailureConflict, but for a status and an end, which say it ended. A checkout
// to a commit whose images are not the lab's is core.FailureConflict, naming what differs.
func (a *App) Lab(ctx context.Context, c core.CallContext, id core.JobID, call core.LabCall, emit func(core.LabReply) error) error {
	scope := c.Scope
	st, err := a.open(ctx, scope)
	if err != nil {
		return err
	}
	ref := lifecycle.Ref{Store: storeIDOf(scope), Job: id}
	for {
		wake := a.jobsChanged.Wait()
		job, err := a.readJob(ctx, st, id)
		if err != nil {
			return err
		}
		if command, ok := job.Spec.Settings.(core.CommandSettings); !ok || command.Lab == nil {
			return invalid("job %s is a %s job, not a lab", id, job.Spec.Settings.Kind())
		}
		asked, status := call.(core.LabStatusCall)
		status = status && !(asked.Wait && job.State == core.JobQueued)
		_, end := call.(core.LabEndCall)
		switch {
		case job.State.Final() && (status || end):
			return emit(core.LabStatus{Phase: core.LabEnded, Commit: job.Spec.Commit})
		case job.State.Final():
			return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(
				"lab %s has ended (%s); its artifacts are kept with it, and lab start begins another", id, job.State)}
		case job.State == core.JobQueued && status:
			return emit(core.LabStatus{Phase: core.LabQueued, Commit: job.Spec.Commit})
		case job.State == core.JobQueued && end:
			if _, err := a.Cancel(ctx, c, id, "the lab was ended before it started"); err != nil {
				return err
			}
			return emit(core.LabStatus{Phase: core.LabEnded, Commit: job.Spec.Commit})
		case job.State == core.JobQueued:
			if detail, steered := a.steeredBack(job); steered {
				return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("lab %s has not started: %s", id, detail)}
			}
		case job.State == core.JobRunning:
			return a.labCall(ctx, c, ref, call, emit)
		}
		if f, waits := a.waitOf(ref); waits {
			return &f
		}
		if err := a.pause(ctx, wake); err != nil {
			return err
		}
	}
}

// steeredBack says why a queued job will not start until someone acts: it is held, or the daemon
// drains.
func (a *App) steeredBack(job core.Job) (string, bool) {
	if job.Hold != nil {
		return job.Hold.WaitDetail(), true
	}
	if d := a.view.Load().drain; d != nil {
		return d.WaitDetail(), true
	}
	return "", false
}

// labCall makes the call on a running lab through a connection of its own to the lab's
// supervisor, which it closes.
func (a *App) labCall(ctx context.Context, c core.CallContext, ref lifecycle.Ref, call core.LabCall, emit func(core.LabReply) error) error {
	run, err := a.cfg.Supervisors.Probe(ctx, ref)
	if err != nil {
		return err
	}
	alive, ok := run.(supervisor.Alive)
	if !ok {
		return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
			"lab %s runs and its supervisor does not answer; the daemon follows it and says how it ended", ref.Job)}
	}
	h := alive.Handle
	defer h.Close()
	if to, ok := call.(core.LabCheckoutCall); ok {
		if err := a.sameImages(ctx, c.Scope, h, to.Commit); err != nil {
			return err
		}
	}
	answered := false
	err = h.Lab(ctx, call, func(r core.LabReply) error {
		switch r.(type) {
		case core.LabOutput, core.LabFile:
		default:
			answered = true
		}
		return emit(r)
	})
	switch {
	case err != nil:
		var g supervisor.Gone
		if errors.As(err, &g) || errors.Is(err, supervisor.ErrGone) {
			return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(
				"lab %s has ended: its supervisor is gone (%v)", ref.Job, err)}
		}
		return err
	case !answered:
		return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("lab %s did not answer the call", ref.Job)}
	}
	if _, end := call.(core.LabEndCall); end {
		// A lab that was not ready yet ends now, as a queued one does: it holds nothing worth
		// waiting for, such as a place in a cluster's queue.
		var s core.LabStatus
		if err := h.Lab(ctx, core.LabStatusCall{}, func(r core.LabReply) error {
			s, _ = r.(core.LabStatus)
			return nil
		}); err == nil && s.Phase == core.LabStarting {
			_, err := a.Cancel(ctx, c, ref.Job, "the lab was ended before it was ready")
			return err
		}
	}
	return nil
}

// sameImages refuses a checkout of a lab to a commit whose images differ from the ones the lab
// runs: the task's, which its version names, and, for a task of two images, the candidate's, which
// the digest of its Dockerfile and build context names. The lab's containers run their images
// from the start to the end, so another image takes another lab.
func (a *App) sameImages(ctx context.Context, scope core.Scope, h supervisor.Handle, to core.Commit) error {
	var now core.LabStatus
	if err := h.Lab(ctx, core.LabStatusCall{}, func(r core.LabReply) error {
		now, _ = r.(core.LabStatus)
		return nil
	}); err != nil {
		return err
	}
	if now.Commit == "" || now.Commit == to {
		return nil
	}
	repository, name := string(scope.Repository()), string(scope.Task())
	was, err := a.taskAt(ctx, repository, now.Commit, name)
	if err != nil {
		return err
	}
	will, err := a.taskAt(ctx, repository, to, name)
	if err != nil {
		return err
	}
	refuse := func(what string) error {
		return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(
			"commit %s has another %s than the lab's commit %s, and a lab keeps the images it started with: start a new lab for it", to, what, now.Commit)}
	}
	if was.Version != will.Version {
		return refuse(fmt.Sprintf("task %q, so another task image,", name))
	}
	if was.Candidate == nil {
		return nil
	}
	if will.Candidate == nil || !sameBuild(was.Candidate.Build, will.Candidate.Build) {
		return refuse("candidate image build")
	}
	if a.cfg.Files == nil {
		return &core.Failure{Kind: core.FailureUnavailable, Message: "this daemon cannot read a commit's files, so it cannot compare the candidate's images of two commits"}
	}
	before, err := a.candidateDigest(ctx, repository, now.Commit, was.Candidate.Build)
	if err != nil {
		return err
	}
	after, err := a.candidateDigest(ctx, repository, to, will.Candidate.Build)
	if err != nil {
		return err
	}
	if before != after {
		return refuse(fmt.Sprintf("candidate image (its Dockerfile %s or build context %s changed)", will.Candidate.Build.Dockerfile, will.Candidate.Build.Context))
	}
	return nil
}

func sameBuild(a, b core.CandidateBuild) bool {
	return a.Dockerfile == b.Dockerfile && a.Context == b.Context
}

// candidateDigest names what builds the candidate's image at the commit.
func (a *App) candidateDigest(ctx context.Context, repository string, commit core.Commit, b core.CandidateBuild) (string, error) {
	tree, err := a.cfg.Files.Files(ctx, repository, commit, task.CandidatePaths(b))
	if err != nil {
		return "", err
	}
	c, err := task.ReadCandidate(tree, b)
	if err != nil {
		return "", err
	}
	return c.Digest, nil
}
