package supervisor

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"slices"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
)

// InProcess is Supervisors whose supervisors are Supervisor values in the daemon's own process.
// It is a real implementation of the interface, with one difference: a "restart" is a new
// InProcess built over the same Survivors, which stands for the processes that outlive a daemon.
type InProcess struct {
	env       environment.Environments
	checkouts Checkouts
	alive     *Survivors
	host      Host
}

var _ Supervisors = (*InProcess)(nil)

// Survivors are the supervisors that outlive one InProcess. A test makes one, builds an InProcess
// over it, "restarts the daemon" by building another over the same Survivors, and "reboots the
// machine" with Reboot.
type Survivors struct {
	mu   sync.Mutex
	runs map[lifecycle.Ref]*process
}

// process is one supervisor of InProcess and whether it was killed.
type process struct {
	sup    *Supervisor
	killed chan struct{}
	once   sync.Once
}

func (p *process) kill() {
	p.once.Do(func() { close(p.killed) })
	p.sup.Close()
}

func (p *process) dead() bool {
	select {
	case <-p.killed:
		return true
	default:
		return false
	}
}

// NewSurvivors returns an empty set.
func NewSurvivors() *Survivors { return &Survivors{runs: map[lifecycle.Ref]*process{}} }

// Kill ends the supervisor of a job without letting it report, as a SIGKILL does, and leaves its
// run behind. It returns false for a job that has none.
func (s *Survivors) Kill(ref lifecycle.Ref) bool {
	s.mu.Lock()
	proc, ok := s.runs[ref]
	s.mu.Unlock()
	if ok {
		proc.kill()
	}
	return ok
}

// Reboot kills every supervisor and removes every run, as a machine that restarts does.
func (s *Survivors) Reboot() {
	s.mu.Lock()
	runs := s.runs
	s.runs = map[lifecycle.Ref]*process{}
	s.mu.Unlock()
	for _, proc := range runs {
		proc.kill()
	}
}

// Close ends every command still running, so a test leaves nothing behind.
func (s *Survivors) Close() {
	// A copy, not the map: a daemon that is still settling a job forgets its run during this.
	s.mu.Lock()
	runs := slices.Collect(maps.Values(s.runs))
	s.mu.Unlock()
	for _, proc := range runs {
		proc.sup.Close()
	}
}

// NewInProcess returns Supervisors that run launches in the environments, over the survivors.
func NewInProcess(env environment.Environments, checkouts Checkouts, alive *Survivors) *InProcess {
	return &InProcess{env: env, checkouts: checkouts, alive: alive}
}

// WithHost gives the supervisors the machine that check and measure jobs run on, and returns p. A
// supervisor of a job with a plan that has none refuses the job as unavailable.
func (p *InProcess) WithHost(h Host) *InProcess {
	p.host = h
	return p
}

// Spawn implements Supervisors.
func (p *InProcess) Spawn(ctx context.Context, launch lifecycle.Launch) (Handle, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	ref := launch.Ref()
	p.alive.mu.Lock()
	defer p.alive.mu.Unlock()
	if _, ok := p.alive.runs[ref]; ok {
		return nil, &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("job %s already has a run", ref)}
	}
	proc := &process{killed: make(chan struct{}),
		sup: New(Config{Launch: launch, Environments: p.env, Checkouts: p.checkouts, Spool: &MemorySpool{}, Host: p.host})}
	p.alive.runs[ref] = proc
	return &inProcessHandle{proc: proc}, nil
}

// Runs implements Supervisors. A killed supervisor's run is reported as gone.
func (p *InProcess) Runs(ctx context.Context) ([]Run, error) {
	p.alive.mu.Lock()
	defer p.alive.mu.Unlock()
	var out []Run
	for ref, proc := range p.alive.runs {
		if proc.dead() {
			out = append(out, Run{Ref: ref, Presence: lifecycle.PresenceGone})
			continue
		}
		out = append(out, Run{Ref: ref, Presence: lifecycle.PresenceAlive, Handle: &inProcessHandle{proc: proc}})
	}
	return out, nil
}

// Forget implements Supervisors.
func (p *InProcess) Forget(ctx context.Context, ref lifecycle.Ref) error {
	p.alive.mu.Lock()
	defer p.alive.mu.Unlock()
	proc, ok := p.alive.runs[ref]
	if !ok {
		return nil
	}
	if !proc.dead() && proc.sup.Phase() != PhaseReleased {
		return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("job %s still has a supervisor", ref)}
	}
	delete(p.alive.runs, ref)
	return nil
}

type inProcessHandle struct{ proc *process }

var _ Handle = (*inProcessHandle)(nil)

func (h *inProcessHandle) check(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if h.proc.dead() || h.proc.sup.Phase() == PhaseReleased {
		return ErrGone
	}
	return nil
}

func (h *inProcessHandle) Start(ctx context.Context) error {
	if err := h.check(ctx); err != nil {
		return err
	}
	return h.proc.sup.Start()
}

func (h *inProcessHandle) Cancel(ctx context.Context, reason string) error {
	if err := h.check(ctx); err != nil {
		return err
	}
	h.proc.sup.Cancel(reason)
	return nil
}

func (h *inProcessHandle) Follow(ctx context.Context, offset uint64, emit func([]byte) error) (lifecycle.Outcome, error) {
	if err := h.check(ctx); err != nil {
		return lifecycle.Outcome{}, err
	}
	// A kill ends a waiting Attach: the connection to a killed process drops.
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() {
		select {
		case <-h.proc.killed:
			cancel()
		case <-ctx.Done():
		}
	}()
	out, err := h.proc.sup.Attach(ctx, offset, func(_ uint64, data []byte) error { return emit(data) })
	if h.proc.dead() || errors.Is(err, ErrReleased) {
		return lifecycle.Outcome{}, ErrGone
	}
	return out, err
}

func (h *inProcessHandle) Release(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if h.proc.dead() {
		return ErrGone
	}
	return h.proc.sup.Release()
}
