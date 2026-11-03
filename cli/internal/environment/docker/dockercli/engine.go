package dockercli

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// DefaultProgram is the docker program, found on PATH.
const DefaultProgram = "docker"

// DefaultCallTimeout bounds each call that should answer at once.
const DefaultCallTimeout = 2 * time.Minute

// Config configures New.
type Config struct {
	// Program is the docker program to run. Empty means DefaultProgram.
	Program string
	// Timers makes the bound of each call. Required.
	Timers clock.Timers
	// CallTimeout is the bound of a call that should answer at once. Zero means
	// DefaultCallTimeout.
	CallTimeout time.Duration
	// Shell is the machine the docker program runs on. Nil means this one.
	Shell remote.Shell
	// Allocator, when set, places a container's first process and an image build in an
	// allocation of the machine's scheduler.
	Allocator Allocator
	// StartTimeout bounds the wait for a container to start, which includes the wait for its
	// allocation. Zero means CallTimeout.
	StartTimeout time.Duration
}

// Allocator places the docker commands that hold the machine, a container's first process and an
// image build, in an allocation of the machine's scheduler, by rewriting their command lines.
type Allocator interface {
	Allocate(a Allocation, argv []string) []string
}

// Allocation is what a command placed in an allocation is.
type Allocation struct {
	// Name is unique to the command: the container's name, or the build's.
	Name string
	// Scope is the scope of the container, which a sweep of the scope looks for.
	Scope string
	// Limit is the longest the command may run; zero means the caller gave none.
	Limit time.Duration
	// Build says the command builds an image.
	Build bool
}

// Engine is a docker.Engine over the docker program. Create it with New. It is safe for
// concurrent use.
type Engine struct {
	program      string
	timers       clock.Timers
	timeout      time.Duration
	startTimeout time.Duration
	shell        remote.Shell
	allocator    Allocator
}

var _ docker.Engine = (*Engine)(nil)

// New returns an Engine, or a core.FailureInvalidInput that names what is missing.
func New(cfg Config) (*Engine, error) {
	if cfg.Timers == nil {
		return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: "dockercli: the config has no Timers"}
	}
	e := &Engine{program: cfg.Program, timers: cfg.Timers, timeout: cfg.CallTimeout, startTimeout: cfg.StartTimeout, shell: cfg.Shell, allocator: cfg.Allocator}
	if e.program == "" {
		e.program = DefaultProgram
	}
	if e.timeout <= 0 {
		e.timeout = DefaultCallTimeout
	}
	if e.startTimeout <= 0 {
		e.startTimeout = e.timeout
	}
	if e.shell == nil {
		e.shell = remote.Local{}
	}
	return e, nil
}

// command returns the command that runs docker with args where the engine runs it, placed in an
// allocation when a is set and the engine has an allocator.
func (e *Engine) command(ctx context.Context, a *Allocation, args ...string) *exec.Cmd {
	argv := append([]string{e.program}, args...)
	if a != nil && e.allocator != nil {
		argv = e.allocator.Allocate(*a, argv)
	}
	return e.shell.Command(ctx, argv)
}

// errExpired is the cause of a context whose bound ran out.
var errExpired = errors.New("the call took longer than its bound")

// bound returns ctx limited to the call timeout, and the function that releases it and joins the
// goroutine that watches the timer.
func (e *Engine) bound(ctx context.Context) (context.Context, func()) {
	return e.boundBy(ctx, e.timeout)
}

// boundBy returns ctx limited to d, as bound does.
func (e *Engine) boundBy(ctx context.Context, d time.Duration) (context.Context, func()) {
	fired, stop := e.timers.Timeout(ctx, d)
	bounded, cancel := context.WithCancelCause(ctx)
	var wg sync.WaitGroup
	wg.Go(func() {
		select {
		case <-fired:
			cancel(errExpired)
		case <-bounded.Done():
		}
	})
	return bounded, func() {
		cancel(nil)
		stop()
		wg.Wait()
	}
}

// ended turns the end of a bounded context into the error of the call: the caller's own
// cancellation is the caller's context error, and a bound that ran out is the engine not
// answering.
func (e *Engine) ended(parent, bounded context.Context, verb string) error {
	if err := parent.Err(); err != nil {
		return err
	}
	if errors.Is(context.Cause(bounded), errExpired) {
		return unavailable("docker %s on %s did not answer in time; %s", verb, e.shell.Where(), hint)
	}
	return bounded.Err()
}

// hint is what a user can do when the engine cannot be used.
const hint = "start Docker or give this user access to it, or run with --env host"

func unavailable(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: "docker: " + fmt.Sprintf(format, args...)}
}

// result is what a finished docker command said.
type result struct {
	stdout, stderr string
	code           int
}

// call runs the docker program to its end, bounded, and returns what it said. It fails when the
// program cannot be run at all; a program that ran and failed is a result with a code.
func (e *Engine) call(ctx context.Context, verb string, args ...string) (result, error) {
	bounded, done := e.bound(ctx)
	defer done()
	cmd := e.command(bounded, nil, args...)
	var out, errb bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errb
	err := cmd.Run()
	if bounded.Err() != nil {
		return result{}, e.ended(ctx, bounded, verb)
	}
	res := result{stdout: out.String(), stderr: errb.String()}
	var exit *exec.ExitError
	switch {
	case err == nil:
	case errors.As(err, &exit) && e.shell.Lost(exit.ExitCode()):
		return result{}, unavailable("cannot reach %s to run docker %s: %s", e.shell.Where(), verb, clip(strings.TrimSpace(errb.String())))
	case errors.As(err, &exit):
		res.code = exit.ExitCode()
	case errors.Is(err, exec.ErrNotFound):
		return result{}, unavailable("the %q program was not found (%v); install Docker, or run with --env host", e.program, err)
	default:
		return result{}, unavailable("cannot run %q: %v; %s", e.program, err, hint)
	}
	return res, nil
}

// failed turns a docker command that exited non-zero into the error of the call.
func failed(verb string, res result) error {
	msg := strings.TrimSpace(res.stderr)
	if msg == "" {
		msg = strings.TrimSpace(res.stdout)
	}
	return unavailable("docker %s failed (exit %d): %s; %s", verb, res.code, clip(msg), hint)
}

// clip shortens engine output for a message.
func clip(s string) string {
	const max = 400
	if len(s) <= max {
		return s
	}
	return strings.ToValidUTF8(s[:max], "") + "..."
}

// Image implements docker.Engine.
func (e *Engine) Image(ctx context.Context, ref string) (docker.Image, bool, error) {
	if ref == "" || strings.HasPrefix(ref, "-") {
		return docker.Image{}, false, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("dockercli: %q is not an image reference", ref)}
	}
	res, err := e.call(ctx, "image inspect", "image", "inspect", "--format", "{{.Id}} {{.Os}}/{{.Architecture}}", ref)
	if err != nil {
		return docker.Image{}, false, err
	}
	switch {
	case res.code == 0:
		img, err := parseImage(res.stdout)
		return img, err == nil, err
	case strings.Contains(res.stderr, "No such image") || strings.Contains(res.stderr, "No such object"):
		return docker.Image{}, false, nil
	}
	return docker.Image{}, false, failed("image inspect", res)
}

// Containers implements docker.Engine.
func (e *Engine) Containers(ctx context.Context, match docker.Labels) ([]string, error) {
	args := []string{"ps", "--all", "--no-trunc", "--format", "{{.Names}}"}
	for _, k := range sortedKeys(match) {
		if err := checkLabel(k, match[k]); err != nil {
			return nil, err
		}
		args = append(args, "--filter", "label="+k+"="+match[k])
	}
	res, err := e.call(ctx, "ps", args...)
	if err != nil {
		return nil, err
	}
	if res.code != 0 {
		return nil, failed("ps", res)
	}
	return parseNames(res.stdout), nil
}

// Remove implements docker.Engine.
func (e *Engine) Remove(ctx context.Context, name string) error {
	if err := checkName(name); err != nil {
		return err
	}
	// --volumes removes the anonymous volumes an image's VOLUME lines made, which would otherwise be
	// left dangling by every run.
	res, err := e.call(ctx, "rm", "rm", "--force", "--volumes", name)
	if err != nil {
		return err
	}
	// Engines differ on whether removing what is not there is a failure. It is the state asked for.
	if res.code == 0 || strings.Contains(res.stderr, "No such container") {
		return nil
	}
	return failed("rm", res)
}
