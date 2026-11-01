package fakeenv

import (
	"context"
	"fmt"
	"io"
	"strconv"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// Gates holds the named points a script waits at. A test releases a gate to let the script go on.
type Gates interface {
	// Wait blocks until the gate is released and reports true, or until ctx is done and reports
	// false. A gate released before anyone waits stays released.
	Wait(ctx context.Context, name string) bool
}

// Environment is a fake environment.Environment. Create it with New.
type Environment struct {
	kind  core.EnvironmentKind
	print core.Fingerprint
	gates Gates

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

// WithFingerprintError makes Fingerprint fail with err, as an environment does that cannot be
// reached.
func WithFingerprintError(err error) Option { return func(e *Environment) { e.finger = err } }

// New returns an environment of a kind whose scripts wait at the given gates.
func New(kind core.EnvironmentKind, gates Gates, opts ...Option) *Environment {
	e := &Environment{
		kind:  kind,
		gates: gates,
		print: core.Fingerprint{Kind: kind, Backend: core.BackendCPU, Facts: map[string]string{"os": "fake", "arch": "fake"}},
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

// Run implements environment.Environment by running the command as a script.
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

	if len(x.Argv) == 0 || x.Argv[0] == "" {
		return core.ExecOutcome{}, &core.Failure{Kind: core.FailureInvalidInput, Message: "execution: no command to run"}
	}
	if x.Timeout <= 0 {
		return core.ExecOutcome{}, &core.Failure{Kind: core.FailureInvalidInput, Message: "execution: the timeout must be positive"}
	}
	if x.Checkout.Dir == "" {
		return core.ExecOutcome{}, &core.Failure{Kind: core.FailureInvalidInput, Message: "execution: no checkout directory"}
	}
	if x.Service != nil && len(x.Service.Probe) > 0 && x.Service.Probe[0] == "never" {
		return core.ExecOutcome{Kind: core.ExecServiceNotReady}, nil
	}
	return e.script(ctx, x.Argv, out)
}

func (e *Environment) script(ctx context.Context, argv []string, out io.Writer) (core.ExecOutcome, error) {
	for _, step := range steps(argv) {
		switch step[0] {
		case "print":
			if _, err := fmt.Fprintln(out, strings.Join(step[1:], " ")); err != nil {
				return core.ExecOutcome{Kind: core.ExecUnknown}, err
			}
		case "hold":
			if len(step) != 2 || !e.gates.Wait(ctx, step[1]) {
				return core.ExecOutcome{Kind: core.ExecCancelled}, nil
			}
		case "exit":
			code, err := strconv.Atoi(strings.Join(step[1:], ""))
			if err != nil {
				return core.ExecOutcome{Kind: core.ExecExited, ExitCode: 127}, nil
			}
			return core.ExecOutcome{Kind: core.ExecExited, ExitCode: code}, nil
		case "timeout":
			return core.ExecOutcome{Kind: core.ExecTimedOut}, nil
		case "service-not-ready":
			return core.ExecOutcome{Kind: core.ExecServiceNotReady}, nil
		case "service-exited":
			return core.ExecOutcome{Kind: core.ExecServiceExited}, nil
		case "unknown":
			return core.ExecOutcome{Kind: core.ExecUnknown}, nil
		case "fail":
			return core.ExecOutcome{}, &core.Failure{Kind: core.FailureUnavailable, Message: strings.Join(step[1:], " ")}
		default:
			return core.ExecOutcome{Kind: core.ExecExited, ExitCode: 127}, nil
		}
	}
	return core.ExecOutcome{Kind: core.ExecExited}, nil
}

// steps splits a script at its ";" arguments, dropping empty steps.
func steps(argv []string) [][]string {
	var out [][]string
	var cur []string
	for _, a := range argv {
		if a == ";" {
			if len(cur) > 0 {
				out = append(out, cur)
			}
			cur = nil
			continue
		}
		cur = append(cur, a)
	}
	if len(cur) > 0 {
		out = append(out, cur)
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
func (m Environments) Environment(kind core.EnvironmentKind) (environment.Environment, error) {
	if e, ok := m[kind]; ok {
		return e, nil
	}
	return environment.None{}.Environment(kind)
}
