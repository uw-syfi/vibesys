package supervisor

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"slices"
	"sync"
	"sync/atomic"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/owned"
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
	// rebooted is the runs a reboot took: their record survives, as the real one does outside the
	// runtime directory.
	rebooted map[lifecycle.Ref]rebootedRun
	// wiped is the runs whose directory went away while the machine stayed up.
	wiped map[lifecycle.Ref]bool
}

// process is one supervisor of InProcess and whether it was killed. startSent is the mark the host
// writes in a run's record before it tells the supervisor to start.
type process struct {
	sup       *Supervisor
	launch    lifecycle.Launch
	killed    chan struct{}
	once      sync.Once
	startSent atomic.Bool
}

// start says what is known of whether the process was told to start.
func (p *process) start() lifecycle.Start {
	if p.startSent.Load() {
		return lifecycle.MayHaveStarted{}
	}
	return lifecycle.NeverStarted{}
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

// rebootedRun is the record of a run a reboot took.
type rebootedRun struct {
	launch lifecycle.Launch
	start  lifecycle.Start
}

// NewSurvivors returns an empty set.
func NewSurvivors() *Survivors {
	return &Survivors{
		runs: map[lifecycle.Ref]*process{}, rebooted: map[lifecycle.Ref]rebootedRun{}, wiped: map[lifecycle.Ref]bool{},
	}
}

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

// Reboot kills every supervisor and removes every run but its record, as a machine that restarts
// does.
func (s *Survivors) Reboot() {
	s.mu.Lock()
	runs := s.runs
	s.runs = map[lifecycle.Ref]*process{}
	for ref, proc := range runs {
		s.rebooted[ref] = rebootedRun{launch: proc.launch, start: proc.start()}
	}
	s.mu.Unlock()
	for _, proc := range runs {
		proc.kill()
	}
}

// StartSent says the job's supervisor was told to start, as the record of a run says, and false for
// a job with no run.
func (s *Survivors) StartSent(ref lifecycle.Ref) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if r, ok := s.rebooted[ref]; ok {
		_, maybe := r.start.(lifecycle.MayHaveStarted)
		return maybe
	}
	proc, ok := s.runs[ref]
	return ok && proc.startSent.Load()
}

// Wipe takes a job's run directory away and leaves its supervisor alive, as a logout that clears
// the runtime directory does. It returns false for a job with no run.
func (s *Survivors) Wipe(ref lifecycle.Ref) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.runs[ref]; !ok {
		return false
	}
	s.wiped[ref] = true
	return true
}

// Vanish kills the supervisor of a job and takes every trace of its run away, as a machine gone for
// good does to the daemon of another place, which cannot see that machine. It returns false for a
// job with no run.
func (s *Survivors) Vanish(ref lifecycle.Ref) bool {
	s.mu.Lock()
	proc, ok := s.runs[ref]
	delete(s.runs, ref)
	delete(s.wiped, ref)
	s.mu.Unlock()
	if ok {
		proc.kill()
	}
	return ok
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
	proc := &process{killed: make(chan struct{}), launch: launch,
		sup: New(Config{Launch: launch, Environments: p.env, Checkouts: p.checkouts, Spool: &MemorySpool{}, Host: p.host})}
	p.alive.runs[ref] = proc
	return &inProcessHandle{proc: proc}, nil
}

// Runs implements Supervisors. A killed supervisor's run is reported as gone.
func (p *InProcess) Runs(ctx context.Context) ([]Run, error) {
	p.alive.mu.Lock()
	defer p.alive.mu.Unlock()
	var out []Run
	for ref := range p.alive.runs {
		out = append(out, p.alive.runOf(ref))
	}
	for ref := range p.alive.rebooted {
		out = append(out, p.alive.runOf(ref))
	}
	return out, nil
}

func (s *Survivors) runOf(ref lifecycle.Ref) Run {
	if r, ok := s.rebooted[ref]; ok {
		return Dead{Ref: ref, Proof: lifecycle.ProofRebooted, Start: r.start, Launch: &r.launch}
	}
	proc, ok := s.runs[ref]
	if !ok {
		return None{Ref: ref}
	}
	switch {
	case proc.dead():
		return Dead{Ref: ref, Proof: lifecycle.ProofLockFree, Start: proc.start(), Launch: &proc.launch}
	case s.wiped[ref]:
		return Unknown{Ref: ref, Launch: &proc.launch}
	}
	return Alive{Ref: ref, Handle: &inProcessHandle{proc: proc}, Launch: proc.launch, Phase: proc.sup.Phase()}
}

// Probe implements Supervisors.
func (p *InProcess) Probe(ctx context.Context, ref lifecycle.Ref) (Run, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	p.alive.mu.Lock()
	defer p.alive.mu.Unlock()
	return p.alive.runOf(ref), nil
}

// Forget implements Supervisors.
func (p *InProcess) Forget(ctx context.Context, ref lifecycle.Ref, proof lifecycle.DeathProof) error {
	if !proof.Valid() {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("job %s: nothing proves its supervisor gone", ref)}
	}
	p.alive.mu.Lock()
	defer p.alive.mu.Unlock()
	delete(p.alive.rebooted, ref)
	proc, ok := p.alive.runs[ref]
	if !ok {
		return nil
	}
	if !proc.dead() && proc.sup.Phase() != lifecycle.RunReleased {
		return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("job %s still has a supervisor", ref)}
	}
	delete(p.alive.runs, ref)
	delete(p.alive.wiped, ref)
	return nil
}

type inProcessHandle struct{ proc *process }

var _ Handle = (*inProcessHandle)(nil)

func (h *inProcessHandle) check(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	return h.proc.gone()
}

// gone says how the process is known to be gone, if it is.
func (p *process) gone() error {
	switch {
	case p.dead():
		return Gone{Proof: lifecycle.ProofLockFree, Start: p.start()}
	case p.sup.Phase() == lifecycle.RunReleased:
		return Gone{Proof: lifecycle.ProofReleased}
	}
	return nil
}

func (h *inProcessHandle) Start(ctx context.Context) error {
	if err := h.check(ctx); err != nil {
		return err
	}
	h.proc.startSent.Store(true)
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
	var watcher owned.Group
	defer watcher.Wait()
	defer cancel()
	watcher.Go(func() {
		select {
		case <-h.proc.killed:
			cancel()
		case <-ctx.Done():
		}
	})
	out, err := h.proc.sup.Attach(ctx, offset, func(_ uint64, data []byte) error { return emit(data) })
	if gone := h.proc.gone(); gone != nil && (h.proc.dead() || errors.Is(err, ErrReleased)) {
		return lifecycle.Outcome{}, gone
	}
	return out, err
}

func (h *inProcessHandle) Release(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if h.proc.dead() {
		return Gone{Proof: lifecycle.ProofLockFree, Start: h.proc.start()}
	}
	return h.proc.sup.Release()
}

// Close implements Handle. A connection that is not there has nothing to close.
func (h *inProcessHandle) Close() {}
