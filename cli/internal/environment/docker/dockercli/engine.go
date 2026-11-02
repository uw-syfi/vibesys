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
}

// Engine is a docker.Engine over the docker program. Create it with New. It is safe for
// concurrent use.
type Engine struct {
	program string
	timers  clock.Timers
	timeout time.Duration
}

var _ docker.Engine = (*Engine)(nil)

// New returns an Engine, or a core.FailureInvalidInput that names what is missing.
func New(cfg Config) (*Engine, error) {
	if cfg.Timers == nil {
		return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: "dockercli: the config has no Timers"}
	}
	e := &Engine{program: cfg.Program, timers: cfg.Timers, timeout: cfg.CallTimeout}
	if e.program == "" {
		e.program = DefaultProgram
	}
	if e.timeout <= 0 {
		e.timeout = DefaultCallTimeout
	}
	return e, nil
}

// errExpired is the cause of a context whose bound ran out.
var errExpired = errors.New("the call took longer than its bound")

// bound returns ctx limited to the call timeout, and the function that releases it and joins the
// goroutine that watches the timer.
func (e *Engine) bound(ctx context.Context) (context.Context, func()) {
	fired, stop := e.timers.Timeout(e.timeout)
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
		return unavailable("docker %s did not answer within %v; %s", verb, e.timeout, hint)
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
	cmd := exec.CommandContext(bounded, e.program, args...)
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
	res, err := e.call(ctx, "rm", "rm", "--force", name)
	if err != nil {
		return err
	}
	// Engines differ on whether removing what is not there is a failure. It is the state asked for.
	if res.code == 0 || strings.Contains(res.stderr, "No such container") {
		return nil
	}
	return failed("rm", res)
}
