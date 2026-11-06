package fakeenv

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
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

func (idleTimers) Timeout(context.Context, time.Duration) (<-chan struct{}, func()) {
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
	reader environment.MachineReader

	mu       sync.Mutex
	runs     []core.Execution
	active   int
	running  []*core.Execution
	finger   error
	fingered int
	// hold, when set, is the gate Fingerprint waits at, as a real environment waits for an
	// image build.
	hold string
	// candidates builds a candidate's image; nil builds every one to a digest of its build.
	candidates func(core.CandidateBuild) (core.CandidateImage, error)
	built      []core.CandidateBuild
}

// CandidatePort is the port a fake environment's candidate server is told to listen on.
const CandidatePort = 18080

// WithCandidates sets how the environment builds a candidate's image, such as a build that fails.
// The default builds every one, to an image whose digest is that of its build.
func WithCandidates(build func(core.CandidateBuild) (core.CandidateImage, error)) Option {
	return func(e *Environment) { e.candidates = build }
}

// Candidate implements environment.Environment.
func (e *Environment) Candidate(ctx context.Context, b core.CandidateBuild, log io.Writer) (core.CandidateImage, error) {
	if err := ctx.Err(); err != nil {
		return core.CandidateImage{}, err
	}
	e.mu.Lock()
	e.built = append(e.built, b)
	build := e.candidates
	e.mu.Unlock()
	if e.kind != core.EnvironmentDocker {
		return core.CandidateImage{}, environment.NoImages(e.kind)
	}
	if build != nil {
		return build(b)
	}
	sum := sha256.Sum256([]byte(b.Dockerfile + "\x00" + b.Context))
	img := core.CandidateImage{ID: "sha256:" + hex.EncodeToString(sum[:]), Tag: "vibesys-candidate:fake"}
	if log != nil {
		_, _ = fmt.Fprintf(log, "built %s\n", img.ID)
	}
	return img, nil
}

// CandidatesBuilt returns every build Candidate was asked for, in order.
func (e *Environment) CandidatesBuilt() []core.CandidateBuild {
	e.mu.Lock()
	defer e.mu.Unlock()
	return slices.Clone(e.built)
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

// WithMachine sets the machine the environment's commands run on. Without it reading the
// machine fails as unavailable, which a job records as facts and load it could not read.
func WithMachine(m environment.MachineReader) Option { return func(e *Environment) { e.reader = m } }

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
	hold := e.hold
	e.mu.Unlock()
	if hold != "" {
		if p, ok := e.gates.(Points); ok {
			p.Mark(FingerprintMark)
		}
		if !e.gates.Wait(ctx, hold) {
			return core.Fingerprint{}, ctx.Err()
		}
	}
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

// FingerprintMark is the point Fingerprint marks when it waits at the gate HoldFingerprint names.
const FingerprintMark = "fingerprint-held"

// HoldFingerprint makes every later Fingerprint wait at the gate until it is released or the
// caller's context ends, as an environment that builds an image first does; an empty gate holds
// none.
func (e *Environment) HoldFingerprint(gate string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.hold = gate
}

// Machine implements environment.Environment.
func (e *Environment) Machine(ctx context.Context) (core.MachineFacts, error) {
	if e.reader == nil {
		return core.MachineFacts{}, errNoMachine
	}
	return e.reader.Machine(ctx)
}

// Load implements environment.Environment.
func (e *Environment) Load(ctx context.Context) (core.MachineLoad, error) {
	if e.reader == nil {
		return core.MachineLoad{}, errNoMachine
	}
	return e.reader.Load(ctx)
}

var errNoMachine = &core.Failure{Kind: core.FailureUnavailable, Message: "the fake environment was given no machine"}

// Fingerprinted returns how many times Fingerprint was called.
func (e *Environment) Fingerprinted() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.fingered
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

// ActiveRuns returns the executions of the runs that have started and not returned, in the order
// they started.
func (e *Environment) ActiveRuns() []core.Execution {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := make([]core.Execution, 0, len(e.running))
	for _, x := range e.running {
		out = append(out, *x)
	}
	return out
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
func (e *Environment) Run(ctx context.Context, x core.Execution, out io.Writer) core.Attempt {
	e.mu.Lock()
	e.runs = append(e.runs, x)
	e.active++
	e.running = append(e.running, &x)
	e.mu.Unlock()
	defer func() {
		e.mu.Lock()
		e.active--
		e.running = slices.DeleteFunc(e.running, func(r *core.Execution) bool { return r == &x })
		e.mu.Unlock()
	}()

	if err := environment.Validate(x); err != nil {
		return core.NotStarted{Failure: core.AsFailure(err)}
	}
	if x.Candidate != nil && e.kind != core.EnvironmentDocker {
		return core.NotStarted{Failure: core.AsFailure(environment.NoImages(e.kind))}
	}
	if ctx.Err() != nil {
		return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	}
	if out == nil {
		out = io.Discard
	}
	limit, stop := e.timers.Timeout(ctx, x.Timeout)
	defer stop()
	var wg sync.WaitGroup
	// Several processes of the script write to out at once. The candidate's server, when the
	// execution has one, runs on the same fake machine as the command: they share its network.
	l := &launcher{in: &interpreter{gates: e.gates, dir: x.Checkout.Dir, files: e.files, ready: &atomic.Bool{}}, wg: &wg}
	out = &syncWriter{w: out}
	ev := environment.Events{
		Limit:     limit,
		Cancelled: ctx.Done(),
		Pause:     func() (<-chan struct{}, func()) { return e.timers.Timeout(ctx, e.pause) },
		Grace:     func() (<-chan struct{}, func()) { return e.timers.Timeout(ctx, environment.DefaultGrace) },
	}
	places := environment.Places{Command: l, Service: l}
	tail := &environment.Tail{}
	if c := x.Candidate; c != nil && c.Serve != nil {
		startup, stopStartup := e.timers.Timeout(ctx, c.Serve.StartupTimeout)
		defer stopStartup()
		ev.Startup = startup
		x.Service = &core.ServiceSpec{Argv: environment.ResolvePort(c.Serve.Argv, CandidatePort), Probe: []string{ProbeReady}}
		places.Service = environment.Teeing(l, tail)
	}
	attempt := environment.ExecuteBeside(x, places, ev, out)
	l.end()
	return environment.WithServiceLog(environment.Finish(ctx, attempt, nil), tail)
}

// Lab implements environment.Environment: its rooms are launchers of scripts on one fake machine,
// which share its files, so the artifacts directory is where the caller has it and nothing is
// copied. The candidate's room listens on CandidatePort.
func (e *Environment) Lab(ctx context.Context, x core.Execution, out io.Writer, hold environment.HoldLab) core.Attempt {
	e.mu.Lock()
	e.runs = append(e.runs, x)
	e.active++
	e.running = append(e.running, &x)
	e.mu.Unlock()
	defer func() {
		e.mu.Lock()
		e.active--
		e.running = slices.DeleteFunc(e.running, func(r *core.Execution) bool { return r == &x })
		e.mu.Unlock()
	}()
	if err := environment.ValidateLab(x); err != nil {
		return core.NotStarted{Failure: core.AsFailure(err)}
	}
	if e.kind != core.EnvironmentDocker {
		return environment.NoLab(e.kind)
	}
	if ctx.Err() != nil {
		return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	}
	limit, stop := e.timers.Timeout(ctx, x.Timeout)
	defer stop()
	// Each room waits for its own processes, so that ending one never waits on another's.
	in := &interpreter{gates: e.gates, dir: x.Checkout.Dir, files: e.files, ready: &atomic.Bool{}}
	rooms := map[core.LabRoom]*launcher{core.LabCandidate: {in: in, wg: &sync.WaitGroup{}}}
	if x.Candidate != nil {
		rooms[core.LabTask] = &launcher{in: in, wg: &sync.WaitGroup{}}
	}
	bench := environment.Bench{
		Rooms: map[core.LabRoom]environment.Launcher{}, Port: CandidatePort, Artifacts: x.Artifacts,
		Limit: limit, Cancelled: ctx.Done(),
		Grace:    func() (<-chan struct{}, func()) { return e.timers.Timeout(ctx, environment.DefaultGrace) },
		Collect:  func(ctx context.Context) error { return ctx.Err() },
		Replaced: func(ctx context.Context) error { return ctx.Err() },
		Added:    func(ctx context.Context, _ core.LabPath) error { return ctx.Err() },
	}
	for room, l := range rooms {
		bench.Rooms[room] = l
	}
	ended := hold(bench)
	for _, l := range rooms {
		l.end()
	}
	return environment.Finish(ctx, core.Ended{Exec: ended}, nil)
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
