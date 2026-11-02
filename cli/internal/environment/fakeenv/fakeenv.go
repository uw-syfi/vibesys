package fakeenv

import (
	"context"
	"io"
	"strings"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// DefaultProbePause is the pause between two probes of a service that Environment waits on its
// timers for.
const DefaultProbePause = environment.DefaultProbePause

// idleTimers are timers that never fire: an environment given no other has no limit.
type idleTimers struct{}

func (idleTimers) Timeout(time.Duration) (<-chan struct{}, func()) {
	return make(chan struct{}), func() {}
}

// Gates holds the named points a script waits at. A test releases a gate to let the script go on.
type Gates interface {
	// Wait blocks until the gate is released and reports true, or until ctx is done and reports
	// false. A gate released before anyone waits stays released.
	Wait(ctx context.Context, name string) bool
}

// Files is where the `emit` step puts the file a command was told to write. A hostfs.Memory is
// one: the job flow reads the same files back from it.
type Files interface {
	Put(path string, data []byte)
}

// Environment is a fake environment.Environment. Create it with New.
type Environment struct {
	files  Files
	kind   core.EnvironmentKind
	print  core.Fingerprint
	gates  Gates
	timers clock.Timers
	pause  time.Duration

	mu       sync.Mutex
	runs     []core.Execution
	active   int
	finger   error
	fingered int
}

var _ environment.Environment = (*Environment)(nil)

// Option configures New.
type Option func(*Environment)

// WithFingerprint sets the fingerprint the environment reports. The default is a host
// environment on the CPU backend with a fixed set of facts.
func WithFingerprint(f core.Fingerprint) Option { return func(e *Environment) { e.print = f } }

// WithFiles sets where `emit` writes. Without it `emit` exits with code 127, as a command does
// that is told to write a file it cannot.
func WithFiles(f Files) Option { return func(e *Environment) { e.files = f } }

// WithTimers sets the timers the environment takes its limit and its probe pauses from. The
// default timers never fire. A test that wants a run to reach its limit supplies timers it fires.
func WithTimers(t clock.Timers) Option { return func(e *Environment) { e.timers = t } }

// WithProbePause sets the pause between two probes of a service. The default is
// DefaultProbePause.
func WithProbePause(d time.Duration) Option { return func(e *Environment) { e.pause = d } }

// WithFingerprintError makes Fingerprint fail with err, as an environment does that cannot be
// reached.
func WithFingerprintError(err error) Option { return func(e *Environment) { e.finger = err } }

// New returns an environment of a kind whose scripts wait at the given gates.
func New(kind core.EnvironmentKind, gates Gates, opts ...Option) *Environment {
	e := &Environment{
		kind:   kind,
		gates:  gates,
		timers: idleTimers{},
		pause:  DefaultProbePause,
		print:  core.Fingerprint{Kind: kind, Backend: core.BackendCPU, Facts: map[string]string{"os": "fake", "arch": "fake"}},
	}
	if kind == core.EnvironmentDocker {
		e.print.ImageDigest = "sha256:" + strings.Repeat("0", 64)
	}
	for _, opt := range opts {
		opt(e)
	}
	return e
}

// Kind implements environment.Environment.
func (e *Environment) Kind() core.EnvironmentKind { return e.kind }

// Fingerprint implements environment.Environment.
func (e *Environment) Fingerprint(ctx context.Context) (core.Fingerprint, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.fingered++
	if e.finger != nil {
		return core.Fingerprint{}, e.finger
	}
	f := e.print
	f.Facts = cloneFacts(f.Facts)
	return f, nil
}

// Runs returns every execution Run was given, in order.
func (e *Environment) Runs() []core.Execution {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]core.Execution(nil), e.runs...)
}

// Active returns how many runs have started and not returned. A test that finishes with a run
// still active has leaked a command.
func (e *Environment) Active() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.active
}

func cloneFacts(m map[string]string) map[string]string {
	if m == nil {
		return nil
	}
	out := make(map[string]string, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

// Environments is an environment.Environments holding one fake environment per kind.
type Environments map[core.EnvironmentKind]*Environment

var _ environment.Environments = Environments(nil)

// Environment implements environment.Environments.
func (m Environments) Environment(kind core.EnvironmentKind, target environment.Target) (environment.Environment, error) {
	if e, ok := m[kind]; ok {
		return e, nil
	}
	return environment.None{}.Environment(kind, target)
}

// Run implements environment.Environment by running the command as a script, and its service as
// another. Every process of a script is a goroutine of a group, and the group ends on every path
// as the contract requires; Run returns when they all have.
func (e *Environment) Run(ctx context.Context, x core.Execution, out io.Writer) (core.ExecOutcome, error) {
	e.mu.Lock()
	e.runs = append(e.runs, x)
	e.active++
	e.mu.Unlock()
	defer func() {
		e.mu.Lock()
		e.active--
		e.mu.Unlock()
	}()

	if err := environment.Validate(x); err != nil {
		return core.ExecOutcome{}, err
	}
	if ctx.Err() != nil {
		return core.ExecOutcome{Kind: core.ExecCancelled}, nil
	}
	if out == nil {
		out = io.Discard
	}
	limit, stop := e.timers.Timeout(x.Timeout)
	defer stop()
	var wg sync.WaitGroup
	// Several processes of the script write to out at once.
	l := &launcher{in: &interpreter{gates: e.gates, dir: x.Checkout.Dir, files: e.files}, wg: &wg}
	out = &syncWriter{w: out}
	defer l.end()
	return environment.Execute(x, l, environment.Events{
		Limit:     limit,
		Cancelled: ctx.Done(),
		Pause:     func() (<-chan struct{}, func()) { return e.timers.Timeout(e.pause) },
	}, out)
}

// syncWriter serializes the writes of the several processes of one run.
type syncWriter struct {
	mu sync.Mutex
	w  io.Writer
}

func (s *syncWriter) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.w.Write(p)
}
