package supervisor

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/changed"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/flowrun"
	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
	"github.com/uw-syfi/vibesys/cli/internal/lab"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/owned"
)

// ErrReleased ends an Attach on a supervisor that was released while it waited.
var ErrReleased = errors.New("the supervisor was released")

// Host is what a check or a measure job needs of the daemon's machine besides its environment,
// which reads the machine the job runs on: the file system for the benchmark's result files, and
// the time. A supervisor of a command job does not use it.
type Host struct {
	Files hostfs.FS
	Clock clock.Clock
	// Artifacts, when set, names the artifacts directory of a job, which outlives it. A job of a
	// host with none keeps no artifacts.
	Artifacts func(repository, task string, job core.JobID) string
}

// Config is what a Supervisor needs. Every field is required, except Host for a launch that has
// no plan.
type Config struct {
	Launch       lifecycle.Launch
	Environments environment.Environments
	Checkouts    Checkouts
	Spool        Spool
	Host         Host
}

// Supervisor runs one job's command and keeps what the daemon needs to learn about it. Create it
// with New. All its methods are safe for concurrent use.
type Supervisor struct {
	cfg Config

	// ctx ends the command when the supervisor is closed or a cancel arrives; it is separate
	// from every caller's context so that a daemon going away never ends a job.
	ctx    context.Context
	cancel context.CancelFunc

	group    owned.Group
	changed  changed.Signal
	released chan struct{}
	done     chan struct{}

	// lab is the lab the job is, and nil for a job that is not one.
	lab *lab.Lab

	mu           sync.Mutex
	phase        lifecycle.RunPhase
	cancelAsked  bool
	cancelReason string
	outcome      lifecycle.Outcome
	// checkout is the lab's checkout once it is made, which a lab's Switch replaces in place.
	checkout core.Checkout
	// candidate is the candidate's image the lab's containers run, for a lab of a task of two
	// images, once it is built.
	candidate *core.CandidateImage
}

// New returns a parked supervisor for the launch.
func New(cfg Config) *Supervisor {
	ctx, cancel := context.WithCancel(context.Background())
	s := &Supervisor{
		cfg: cfg, ctx: ctx, cancel: cancel,
		phase: lifecycle.RunParked, released: make(chan struct{}), done: make(chan struct{}),
	}
	if command, ok := cfg.Launch.Spec.Settings.(core.CommandSettings); ok && command.Lab != nil {
		l := cfg.Launch
		s.lab = lab.New(lab.Config{
			Clock: cfg.Host.Clock, Log: writer{s}, Files: cfg.Host.Files, Artifacts: s.labArtifacts(),
			Commit: l.Spec.Commit, Time: command.Timeout,
			Switch: func(ctx context.Context, commit core.Commit) error {
				if err := s.sameCandidate(ctx, commit); err != nil {
					return err
				}
				s.mu.Lock()
				co := s.checkout
				s.mu.Unlock()
				return cfg.Checkouts.Switch(ctx, l.Repository, co, commit)
			},
		})
	}
	return s
}

// labArtifacts is the artifacts directory of the lab's one run, and empty for a supervisor whose
// host keeps no artifacts.
func (s *Supervisor) labArtifacts() string {
	h, l := s.cfg.Host, s.cfg.Launch
	if h.Artifacts == nil {
		return ""
	}
	return flowrun.ArtifactsOf(h.Artifacts(l.Repository, l.Task, l.Job), 1, "lab")
}

// Lab makes one call on the job's lab (Handle.Lab).
func (s *Supervisor) Lab(ctx context.Context, call core.LabCall, emit func(core.LabReply) error) error {
	if s.lab == nil {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("job %s is not a lab", s.cfg.Launch.Job)}
	}
	if s.Phase() == lifecycle.RunReleased {
		return ErrReleased
	}
	return s.lab.Call(ctx, call, emit)
}

// Phase returns where the supervisor is.
func (s *Supervisor) Phase() lifecycle.RunPhase {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.phase
}

// Released is closed once the supervisor is released, which is when its process may exit.
func (s *Supervisor) Released() <-chan struct{} { return s.released }

// Start begins the command. A started supervisor answers the same way, so a daemon that is not
// sure whether its Start arrived may send it again.
func (s *Supervisor) Start() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch s.phase {
	case lifecycle.RunRunning, lifecycle.RunFinished:
		return nil
	case lifecycle.RunReleased:
		return &core.Failure{Kind: core.FailureConflict, Message: "the supervisor was released and cannot start"}
	case lifecycle.RunParked:
	}
	s.phase = lifecycle.RunRunning
	s.group.Go(s.run)
	return nil
}

// Cancel records a request to end the command and ends it. The first reason wins, and a request
// that arrives after the command ended changes nothing. A request that arrives before Start
// stops the command from ever running.
func (s *Supervisor) Cancel(reason string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.phase == lifecycle.RunFinished || s.phase == lifecycle.RunReleased || s.cancelAsked {
		return
	}
	s.cancelAsked, s.cancelReason = true, reason
	s.cancel()
}

// Release lets the supervisor go. It refuses a running command: that is cancelled first and
// released once its outcome is recorded. It is idempotent.
func (s *Supervisor) Release() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch s.phase {
	case lifecycle.RunReleased:
		return nil
	case lifecycle.RunRunning:
		return &core.Failure{Kind: core.FailureConflict, Message: "the job is running; cancel it and release it after its outcome is recorded"}
	case lifecycle.RunParked, lifecycle.RunFinished:
	}
	s.phase = lifecycle.RunReleased
	s.cancel()
	close(s.released)
	s.changed.Notify()
	return nil
}

// Close ends the command, if one runs, and waits until it is gone. A supervisor process calls it
// before it exits, so nothing outlives it. It does not release the supervisor.
func (s *Supervisor) Close() {
	s.cancel()
	s.mu.Lock()
	running := s.phase == lifecycle.RunRunning
	s.mu.Unlock()
	if running {
		<-s.done
	}
}

// Attach passes the output from the offset to emit, in order and each byte once, and returns the
// outcome when the command has ended and every byte was passed. Offsets are in the job's output,
// which starts this attempt's at the launch's OutputBase: an offset before it fails with
// core.FailureInvalidInput. It waits as long as the command runs, and stops with ctx's error, with
// ErrReleased if the supervisor is released, or with emit's error.
func (s *Supervisor) Attach(ctx context.Context, offset uint64, emit func(offset uint64, data []byte) error) (lifecycle.Outcome, error) {
	base := s.cfg.Launch.OutputBase
	if offset < base {
		return lifecycle.Outcome{}, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
			"the job's output from offset %d was asked for, and this attempt's starts at %d", offset, base)}
	}
	buf := make([]byte, 32<<10)
	pos := offset - base
	for {
		wake := s.changed.Wait()
		s.mu.Lock()
		phase, outcome := s.phase, s.outcome
		s.mu.Unlock()
		if phase == lifecycle.RunReleased {
			return lifecycle.Outcome{}, ErrReleased
		}
		// The phase was read before the drain: a finished command wrote every byte already, so
		// draining to the end passes all of them before the outcome.
		for {
			n, err := s.cfg.Spool.ReadAt(buf, pos)
			if n > 0 {
				if err := emit(base+pos, buf[:n]); err != nil {
					return lifecycle.Outcome{}, err
				}
				pos += uint64(n)
				continue
			}
			if err != nil && !errors.Is(err, io.EOF) {
				return lifecycle.Outcome{}, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("reading the job's output: %v", err)}
			}
			break
		}
		if phase == lifecycle.RunFinished {
			return outcome, nil
		}
		select {
		case <-wake:
		case <-ctx.Done():
			return lifecycle.Outcome{}, ctx.Err()
		}
	}
}

func (s *Supervisor) run() {
	defer close(s.done)
	outcome := s.execute()
	s.mu.Lock()
	outcome.CancelAsked, outcome.CancelReason = s.cancelAsked, s.cancelReason
	s.outcome = outcome
	if s.phase == lifecycle.RunRunning {
		s.phase = lifecycle.RunFinished
	}
	s.mu.Unlock()
	s.changed.Notify()
}

// execute runs the command once and says how it ended.
func (s *Supervisor) execute() lifecycle.Outcome {
	s.mu.Lock()
	asked := s.cancelAsked
	s.mu.Unlock()
	cancelled := core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	spec := s.cfg.Launch.Spec
	if plan := s.cfg.Launch.Plan; plan != nil {
		if asked {
			// Cancelled while parked: no step starts.
			return lifecycle.Outcome{End: lifecycle.FlowEnded{Verdict: lifecycle.Stopped{Exec: cancelled.Exec}}}
		}
		return lifecycle.Outcome{End: lifecycle.FlowEnded{Verdict: s.executePlan(*plan)}}
	}
	if asked {
		// Cancelled while parked: the command never starts.
		if s.lab != nil {
			s.lab.Close()
		}
		return commandEnded(cancelled)
	}
	if s.lab != nil {
		attempt := s.executeLab(spec)
		s.lab.Close()
		return commandEnded(attempt)
	}
	return commandEnded(s.executeCommand(spec))
}

// executeCommand runs a command job's command once and says what came of it.
func (s *Supervisor) executeCommand(spec core.JobSpec) core.Attempt {
	command, ok := spec.Settings.(core.CommandSettings)
	if !ok {
		return core.NotStarted{Failure: core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("the launch of a %s job carries no plan", spec.Settings.Kind())}}
	}
	env, err := s.cfg.Environments.Environment(spec.Environment, s.target())
	if err != nil {
		return core.NotStarted{Failure: core.AsFailure(err)}
	}
	checkout, remove, err := s.cfg.Checkouts.Checkout(s.ctx, s.cfg.Launch.Repository, spec.Commit)
	if err != nil {
		return environment.Finish(s.ctx, core.NotStarted{Failure: core.AsFailure(err)}, nil)
	}
	defer remove()
	x := core.Execution{Checkout: checkout, Argv: command.Argv, Service: command.Service, Timeout: command.Timeout}
	if c := command.Candidate; c != nil {
		// A job of a task of two images: its candidate's image first, built from the job's commit.
		img, err := env.Candidate(s.ctx, c.Build, writer{s})
		if err != nil {
			return environment.Finish(s.ctx, core.NotStarted{Failure: core.AsFailure(err)}, nil)
		}
		x.Candidate = &core.CandidateRun{Image: img.ID}
		if c.Serve != nil {
			// A profile: the task's load in its image, against the server under the wrapper. Both
			// are the task's commands, which name the checkout's root.
			serve := *c.Serve
			serve.Argv = core.ResolveProjectRoot(serve.Argv, checkout.Dir)
			x.Candidate.Serve = &serve
			x.Argv = core.ResolveProjectRoot(x.Argv, checkout.Dir)
		}
	}
	if h := s.cfg.Host; h.Artifacts != nil && h.Files != nil {
		x.Artifacts = flowrun.ArtifactsOf(h.Artifacts(s.cfg.Launch.Repository, s.cfg.Launch.Task, s.cfg.Launch.Job), 1, "command")
		if err := h.Files.MakeDir(s.ctx, x.Artifacts); err != nil {
			return core.NotStarted{Failure: core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("making the artifacts directory: %v", err)}}
		}
		_, _ = fmt.Fprintf(writer{s}, "[command] artifacts: %s\n", x.Artifacts)
	}
	return env.Run(s.ctx, x, writer{s})
}

// executeLab holds the job's lab: its checkout, its candidate's image when its task has two, and
// its containers for its time, with the lab answering the calls on it meanwhile.
func (s *Supervisor) executeLab(spec core.JobSpec) core.Attempt {
	command, _ := spec.Settings.(core.CommandSettings)
	h := s.cfg.Host
	artifacts := s.labArtifacts()
	if h.Files == nil || h.Clock == nil || artifacts == "" {
		return core.NotStarted{Failure: core.Failure{Kind: core.FailureUnavailable, Message: "this supervisor was started without the artifacts directory and the clock a lab needs"}}
	}
	env, err := s.cfg.Environments.Environment(spec.Environment, s.target())
	if err != nil {
		return core.NotStarted{Failure: core.AsFailure(err)}
	}
	checkout, remove, err := s.cfg.Checkouts.Checkout(s.ctx, s.cfg.Launch.Repository, spec.Commit)
	if err != nil {
		return environment.Finish(s.ctx, core.NotStarted{Failure: core.AsFailure(err)}, nil)
	}
	defer remove()
	s.mu.Lock()
	s.checkout = checkout
	s.mu.Unlock()
	x := core.Execution{Checkout: checkout, Artifacts: artifacts, Timeout: command.Timeout}
	if c := command.Candidate; c != nil {
		img, err := env.Candidate(s.ctx, c.Build, writer{s})
		if err != nil {
			return environment.Finish(s.ctx, core.NotStarted{Failure: core.AsFailure(err)}, nil)
		}
		x.Candidate = &core.CandidateRun{Image: img.ID}
		s.mu.Lock()
		s.candidate = &img
		s.mu.Unlock()
	}
	if err := h.Files.MakeDir(s.ctx, artifacts); err != nil {
		return core.NotStarted{Failure: core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("making the artifacts directory: %v", err)}}
	}
	_, _ = fmt.Fprintf(writer{s}, "[lab] artifacts: %s\n", artifacts)
	return env.Lab(s.ctx, x, writer{s}, s.lab.Hold)
}

// sameCandidate refuses, with core.FailureConflict, a commit whose candidate's image a container
// of the lab's candidate's image does not run. It builds that image as the lab's was built, by the
// same environment, on the engine that built the lab's, which finds it already built, or builds it
// from its layers' cache, when the commit changed no file the Dockerfile copies; and compares what
// a container of each runs (core.CandidateImage.SameAs), not what built them. The daemon refused
// already a commit of another task or another Dockerfile or build context path, so the lab's build
// is the commit's.
func (s *Supervisor) sameCandidate(ctx context.Context, commit core.Commit) error {
	s.mu.Lock()
	had := s.candidate
	s.mu.Unlock()
	command, _ := s.cfg.Launch.Spec.Settings.(core.CommandSettings)
	if had == nil || command.Candidate == nil {
		return nil
	}
	b := command.Candidate.Build
	target := s.target()
	was := target.Commit
	target.Commit = commit
	env, err := s.cfg.Environments.Environment(s.cfg.Launch.Spec.Environment, target)
	if err != nil {
		return err
	}
	img, err := env.Candidate(ctx, b, writer{s})
	if err != nil {
		return err
	}
	if !img.SameAs(*had) {
		return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(
			"commit %s has another candidate image (its Dockerfile %s or build context %s changed) than the lab's commit %s, "+
				"and a lab keeps the images it started with: start a new lab for it", commit, b.Dockerfile, b.Context, was)}
	}
	return nil
}

// executePlan drives a check or a measure job through its plan and says how it ended. The plan is
// the daemon's decision, so nothing here chooses what to run.
func (s *Supervisor) executePlan(plan flow.Plan) lifecycle.Verdict {
	h := s.cfg.Host
	if h.Files == nil || h.Clock == nil {
		return lifecycle.NotAttempted{Failure: core.Failure{Kind: core.FailureUnavailable, Message: "this supervisor was started without the machine a check or a measure job needs"}}
	}
	l := s.cfg.Launch
	env, err := s.cfg.Environments.Environment(l.Spec.Environment, s.target())
	if err != nil {
		return lifecycle.NotAttempted{Failure: core.AsFailure(err)}
	}
	cfg := flowrun.Config{
		Repository: l.Repository, Commit: l.Spec.Commit, Environment: env, Checkouts: s.cfg.Checkouts,
		Files: h.Files, Clock: h.Clock, Output: writer{s},
	}
	if h.Artifacts != nil {
		cfg.Artifacts = h.Artifacts(l.Repository, l.Task, l.Job)
	}
	verdict, err := flowrun.Run(s.ctx, cfg, plan)
	if err != nil {
		return lifecycle.NotAttempted{Failure: core.AsFailure(err)}
	}
	switch v := verdict.(type) {
	case flow.Passed:
		return lifecycle.Passed{Result: plan.Record(l.Job, l.Spec.Commit, v.Findings, h.Clock.Now())}
	case flow.NotAttempted:
		return lifecycle.NotAttempted{Failure: v.Failure}
	case flow.Rejected:
		return lifecycle.Rejected{Reason: v.Reason}
	case flow.Stopped:
		// A step that ended unlike a pass: the job's output says which, and the settlement of the
		// exit says the rest.
		_, _ = fmt.Fprintf(writer{s}, "vibesys-cli: %s\n", v.Reason)
		return lifecycle.Stopped{Exec: v.Exec}
	case flow.Lost:
		_, _ = fmt.Fprintf(writer{s}, "vibesys-cli: %s\n", v.Reason)
		return lifecycle.Lost{Failure: v.Failure}
	default:
		return lifecycle.Lost{Failure: core.Failure{Kind: core.FailureInternal, Message: "the flow of the job ended without a verdict"}}
	}
}

// target is what the environment is asked to run commands for: the job's task at its commit.
func (s *Supervisor) target() environment.Target {
	l := s.cfg.Launch
	return environment.Target{Repository: l.Repository, Commit: l.Spec.Commit, Task: l.Task, Placement: l.Spec.Placement}
}

func commandEnded(a core.Attempt) lifecycle.Outcome {
	return lifecycle.Outcome{End: lifecycle.CommandEnded{Attempt: a}}
}

// writer is where the command's output goes: into the spool, and to whoever waits for it.
type writer struct{ s *Supervisor }

func (w writer) Write(p []byte) (int, error) {
	if err := w.s.cfg.Spool.Append(p); err != nil {
		return 0, err
	}
	w.s.changed.Notify()
	return len(p), nil
}
