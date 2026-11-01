package supervisor

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/changed"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
)

// ErrReleased ends an Attach on a supervisor that was released while it waited.
var ErrReleased = errors.New("the supervisor was released")

// Phase is where a supervisor is in its life.
type Phase string

// The phases, in order. A supervisor only moves forward.
const (
	// PhaseParked is a supervisor that has not started the command.
	PhaseParked Phase = "parked"
	// PhaseRunning is a supervisor whose command is running.
	PhaseRunning Phase = "running"
	// PhaseFinished is a supervisor whose command ended. It keeps the outcome and the output
	// until it is released.
	PhaseFinished Phase = "finished"
	// PhaseReleased is a supervisor that was told it may go.
	PhaseReleased Phase = "released"
)

// Config is what a Supervisor needs. Every field is required.
type Config struct {
	Launch       lifecycle.Launch
	Environments environment.Environments
	Checkouts    Checkouts
	Spool        Spool
}

// Supervisor runs one job's command and keeps what the daemon needs to learn about it. Create it
// with New. All its methods are safe for concurrent use.
type Supervisor struct {
	cfg Config

	// ctx ends the command when the supervisor is closed or a cancel arrives; it is separate
	// from every caller's context so that a daemon going away never ends a job.
	ctx    context.Context
	cancel context.CancelFunc

	changed  changed.Signal
	released chan struct{}
	done     chan struct{}

	mu           sync.Mutex
	phase        Phase
	cancelAsked  bool
	cancelReason string
	outcome      lifecycle.Outcome
}

// New returns a parked supervisor for the launch.
func New(cfg Config) *Supervisor {
	ctx, cancel := context.WithCancel(context.Background())
	return &Supervisor{
		cfg: cfg, ctx: ctx, cancel: cancel,
		phase: PhaseParked, released: make(chan struct{}), done: make(chan struct{}),
	}
}

// Phase returns where the supervisor is.
func (s *Supervisor) Phase() Phase {
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
	case PhaseRunning, PhaseFinished:
		return nil
	case PhaseReleased:
		return &core.Failure{Kind: core.FailureConflict, Message: "the supervisor was released and cannot start"}
	}
	s.phase = PhaseRunning
	go s.run()
	return nil
}

// Cancel records a request to end the command and ends it. The first reason wins, and a request
// that arrives after the command ended changes nothing. A request that arrives before Start
// stops the command from ever running.
func (s *Supervisor) Cancel(reason string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.phase == PhaseFinished || s.phase == PhaseReleased || s.cancelAsked {
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
	case PhaseReleased:
		return nil
	case PhaseRunning:
		return &core.Failure{Kind: core.FailureConflict, Message: "the job is running; cancel it and release it after its outcome is recorded"}
	}
	s.phase = PhaseReleased
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
	running := s.phase == PhaseRunning
	s.mu.Unlock()
	if running {
		<-s.done
	}
}

// Attach passes the output from the offset to emit, in order and each byte once, and returns the
// outcome when the command has ended and every byte was passed. It waits as long as the command
// runs, and stops with ctx's error, with ErrReleased if the supervisor is released, or with
// emit's error.
func (s *Supervisor) Attach(ctx context.Context, offset uint64, emit func(offset uint64, data []byte) error) (lifecycle.Outcome, error) {
	buf := make([]byte, 32<<10)
	pos := offset
	for {
		wake := s.changed.Wait()
		s.mu.Lock()
		phase, outcome := s.phase, s.outcome
		s.mu.Unlock()
		if phase == PhaseReleased {
			return lifecycle.Outcome{}, ErrReleased
		}
		// The phase was read before the drain: a finished command wrote every byte already, so
		// draining to the end passes all of them before the outcome.
		for {
			n, err := s.cfg.Spool.ReadAt(buf, pos)
			if n > 0 {
				if err := emit(pos, buf[:n]); err != nil {
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
		if phase == PhaseFinished {
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
	if s.phase == PhaseRunning {
		s.phase = PhaseFinished
	}
	s.mu.Unlock()
	s.changed.Notify()
}

// execute runs the command once and says how it ended.
func (s *Supervisor) execute() lifecycle.Outcome {
	s.mu.Lock()
	asked := s.cancelAsked
	s.mu.Unlock()
	if asked {
		// Cancelled while parked: the command never starts.
		return lifecycle.Outcome{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	}
	spec := s.cfg.Launch.Spec
	command, ok := spec.Settings.(core.CommandSettings)
	if !ok {
		return notAttempted(&core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("this build cannot run %s jobs yet", spec.Settings.Kind())})
	}
	env, err := s.cfg.Environments.Environment(spec.Environment)
	if err != nil {
		return notAttempted(err)
	}
	checkout, remove, err := s.cfg.Checkouts.Checkout(s.ctx, s.cfg.Launch.Repository, spec.Commit)
	if err != nil {
		return s.attempt(core.ExecOutcome{}, err)
	}
	defer remove()
	out, err := env.Run(s.ctx, core.Execution{
		Checkout: checkout, Argv: command.Argv, Service: command.Service, Timeout: command.Timeout,
	}, writer{s})
	return s.attempt(out, err)
}

// attempt turns what the environment returned into an outcome. An error is a command that could
// not be attempted, unless a cancel is what stopped the attempt, which is a cancelled command.
func (s *Supervisor) attempt(out core.ExecOutcome, err error) lifecycle.Outcome {
	if err == nil {
		return lifecycle.Outcome{Exec: out}
	}
	if s.ctx.Err() != nil && errors.Is(err, context.Canceled) {
		return lifecycle.Outcome{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	}
	return notAttempted(err)
}

func notAttempted(err error) lifecycle.Outcome {
	var f *core.Failure
	if !errors.As(err, &f) {
		f = &core.Failure{Kind: core.FailureInternal, Message: err.Error()}
	}
	return lifecycle.Outcome{NotAttempted: f}
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
