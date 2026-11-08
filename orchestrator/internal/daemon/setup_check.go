package daemon

import (
	"context"
	"fmt"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/setup"
)

// checkSetup establishes, before the command reaches Decide, what the core may not find out for itself
// about the two commands that open and close a run's setup, beside what checkStart does for a start.
// The command's digest stays that of what the client sent, and a retry of a command that already has a
// receipt skips the check, so it still gets that receipt.
//
//   - BeginSetup is given its repository in canonical form and the job tool the run's agents use.
//   - CompleteSetup is given the verdict on its task: a fresh read of the repository's setup facts,
//     never the observation the panel shows, the six checks derived from it under the standard policy,
//     and, once they all hold, the task's card at the commit the run will start from and the eval place
//     resolved for it. Decide refuses the completion when a check is unmet. A read that failed, or that
//     found the task unreadable, is no verdict: the call answers Unavailable and nothing is decided, so
//     a read that could not be made never passes.
//
// The verdict is host attested and outside the digest, as a start's facts are.
func (s *Service) checkSetup(ctx context.Context, cmd core.Command) (core.Command, error) {
	if _, operator := cmd.Caller.(core.Operator); !operator {
		// Only the operator may begin or complete a setup; Decide refuses an agent that tries.
		return cmd, nil
	}
	switch b := cmd.Body.(type) {
	case core.BeginSetup:
		if _, recall := s.e.State().Recall(cmd.ID, cmd.Digest); recall != core.RecallNew {
			return cmd, nil
		}
		canonical, err := s.Starts.CheckRepository(b.Repository)
		if err != nil {
			return cmd, startError(err)
		}
		b.Repository = canonical
		if s.JobTool != nil {
			if b.JobTool, err = s.JobTool(ctx); err != nil {
				return cmd, status.Error(codes.FailedPrecondition, err.Error())
			}
		}
		cmd.Body = b
	case core.CompleteSetup:
		if _, recall := s.e.State().Recall(cmd.ID, cmd.Digest); recall != core.RecallNew {
			return cmd, nil
		}
		verdict, err := s.verifySetup(ctx, b)
		if err != nil {
			return cmd, err
		}
		b.Verified = verdict
		cmd.Body = b
	}
	return cmd, nil
}

// verifySetup reads the run's task now and says what holds of it. A run that is not live and in setup
// gets no verdict: the core refuses the command for that, with a receipt, before it reads one.
func (s *Service) verifySetup(ctx context.Context, b core.CompleteSetup) (core.SetupVerdict, error) {
	run, ok := s.e.State().Run(b.Run)
	if !ok || run.Phase.Optimizing() || !run.Status.Live() {
		return core.SetupVerdict{}, nil
	}
	read, err := s.readSetupNow(ctx, run, b.Task)
	if err != nil {
		return core.SetupVerdict{}, err
	}
	result := setup.Derive(read.Facts, setup.Standard)
	verdict := core.SetupVerdict{
		Task: b.Task, TaskVersion: read.Facts.Version, BaseCommit: read.Facts.Base.Commit, Unmet: result.Unmet(),
	}
	if len(verdict.Unmet) > 0 {
		// The core refuses it and names the checks, so no card or place is wanted.
		return verdict, nil
	}
	card, err := s.readCard(ctx, TaskRef{Source: TaskCommitted, Repository: run.Repository, Revision: read.Facts.Base.Commit, Task: b.Task})
	if err != nil {
		return core.SetupVerdict{}, err
	}
	if verdict.Facts, err = s.resolvePlace(ctx, card, b.EvalPlace); err != nil {
		return core.SetupVerdict{}, err
	}
	return verdict, nil
}

// readSetupNow reads the setup facts of the run's task and waits for the answer under the service's
// bound, then puts the read in the run's observation so that the panel is as current as the verdict.
func (s *Service) readSetupNow(ctx context.Context, run core.Run, task string) (setup.Read, error) {
	if s.SetupReads == nil {
		return setup.Read{}, status.Error(codes.FailedPrecondition, "this host cannot read the facts of a setup, so it completes none")
	}
	read, err := bound(s.Bounds, ctx, func(c context.Context) (setup.Read, error) {
		done := make(chan setup.Read, 1)
		s.SetupReads.Start(c, SetupRef{JobTool: run.JobTool, Repository: run.Repository, Task: task}, func(r setup.Read) { done <- r })
		select {
		case r := <-done:
			return r, nil
		case <-c.Done():
			return setup.Read{}, c.Err()
		}
	})
	if err != nil {
		return setup.Read{}, status.Errorf(codes.Unavailable, "read the setup of task %s: %v", task, err)
	}
	if s.e.Setup != nil {
		fresh := read
		fresh.Seq, fresh.Task = s.e.Setup.Next(), task
		s.e.Setup.Put(run.ID, fresh)
	}
	switch read.Kind {
	case setup.ReadFacts:
		return read, nil
	case setup.ReadUnreadable:
		return setup.Read{}, status.Error(codes.Unavailable, fmt.Sprintf("task %s cannot be read: %s", task, read.Detail))
	default:
		return setup.Read{}, status.Errorf(codes.Unavailable, "read the setup of task %s: %s", task, read.Detail)
	}
}
