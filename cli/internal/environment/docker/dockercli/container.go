package dockercli

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"slices"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
)

const (
	// readyLine is what the container's first process prints once it is up.
	readyLine = "vs-ready"
	// keeperScript is the container's first process: it says it is up and then waits for the end
	// of its input, which is the caller going away.
	keeperScript = "echo " + readyLine + "; while read -r _; do :; done"
	// execWrapper runs the command with its standard error merged into its standard output.
	execWrapper = `exec "$@" 2>&1`
)

func asExit(err error, target **exec.ExitError) bool { return errors.As(err, target) }

// runArgs are the arguments of the docker run that starts a container. The spec is validated.
func runArgs(spec docker.ContainerSpec) []string {
	args := []string{
		"run", "--interactive", "--init", "--pull", "never",
		"--name", spec.Name,
		"--user", fmt.Sprintf("%d:%d", spec.User.UID, spec.User.GID),
		"--workdir", spec.Dir,
	}
	for _, k := range sortedKeys(spec.Labels) {
		args = append(args, "--label", k+"="+spec.Labels[k])
	}
	for _, m := range spec.Mounts {
		mount := "type=bind,source=" + m.Host + ",target=" + m.Container
		if m.ReadOnly {
			mount += ",readonly"
		}
		args = append(args, "--mount", mount)
	}
	env := make([]string, 0, len(spec.Env))
	for k, v := range spec.Env {
		env = append(env, k+"="+v)
	}
	slices.Sort(env)
	for _, kv := range env {
		args = append(args, "--env", kv)
	}
	return append(args, "--entrypoint", "/bin/sh", spec.Image, "-c", keeperScript)
}

// Start implements docker.Engine.
func (e *Engine) Start(ctx context.Context, spec docker.ContainerSpec) (docker.Container, error) {
	if err := spec.Validate(); err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err // a call that cannot be answered starts nothing
	}
	// The container's input is a pipe only this process holds the write end of.
	inRead, inWrite, err := os.Pipe()
	if err != nil {
		return nil, unavailable("cannot make the pipe that guards the container: %v", err)
	}
	outRead, outWrite, err := os.Pipe()
	if err != nil {
		inRead.Close()
		inWrite.Close()
		return nil, unavailable("cannot make the pipe that guards the container: %v", err)
	}
	h := &handle{e: e, name: spec.Name, stdin: inWrite, keeperDone: make(chan struct{})}
	cmd := exec.Command(e.program, runArgs(spec)...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = inRead, outWrite, &h.stderr
	startErr := cmd.Start()
	inRead.Close()
	outWrite.Close()
	if startErr != nil {
		inWrite.Close()
		outRead.Close()
		return nil, unavailable("cannot run %q: %v; %s", e.program, startErr, hint)
	}
	h.keeper = cmd

	ready := make(chan bool, 1)
	h.wg.Go(func() {
		defer outRead.Close()
		line, _ := bufio.NewReader(outRead).ReadString('\n')
		ready <- strings.TrimSpace(line) == readyLine
		_, _ = io.Copy(io.Discard, outRead)
	})
	h.wg.Go(func() {
		_ = cmd.Wait()
		close(h.keeperDone)
	})

	bounded, done := e.bound(ctx)
	defer done()
	select {
	case ok := <-ready:
		if ok {
			return h, nil
		}
	case <-h.keeperDone:
	case <-bounded.Done():
	}
	// The container did not come up: end the client, and with it the container's input.
	h.Close()
	if bounded.Err() != nil {
		return nil, e.ended(ctx, bounded, "run")
	}
	return nil, startFailure(h.stderr.String())
}

// startFailure turns what a failed docker run said into the error of Start.
func startFailure(stderr string) error {
	msg := strings.TrimSpace(stderr)
	switch {
	case strings.Contains(msg, "is already in use by container") || strings.Contains(msg, "Conflict"):
		return &core.Failure{Kind: core.FailureConflict, Message: "docker: " + clip(msg)}
	case strings.Contains(msg, "No such image"):
		return &core.Failure{Kind: core.FailureNotFound, Message: "docker: " + clip(msg)}
	case strings.Contains(msg, "/bin/sh") && (strings.Contains(msg, "No such file") || strings.Contains(msg, "not found") || strings.Contains(msg, "no such file")):
		return &core.Failure{Kind: core.FailureUnavailable, Message: "docker: the task's image has no /bin/sh, which a run needs to keep its container and to run commands in it; add a shell to the image, or run the task on the host with --env host"}
	}
	return unavailable("starting the container failed: %s; %s", clip(msg), hint)
}

// handle is a started container, and a docker.Container.
type handle struct {
	e      *Engine
	name   string
	stdin  *os.File
	keeper *exec.Cmd
	stderr lockedBuffer

	keeperDone chan struct{}
	wg         sync.WaitGroup // the goroutines this handle owns

	mu     sync.Mutex
	procs  []*proc
	closed bool
	once   sync.Once
}

var _ docker.Container = (*handle)(nil)

// proc is one command in the container.
type proc struct {
	cmd    *exec.Cmd
	exited chan environment.Exit
}

// Exited implements environment.Process.
func (p *proc) Exited() <-chan environment.Exit { return p.exited }

// Kill implements environment.Process. It ends the docker client of the command at once; the
// command itself ends no later than the container's removal, which `docker exec` cannot hasten.
func (p *proc) Kill() {
	if p.cmd.Process != nil {
		_ = p.cmd.Process.Kill()
	}
}

// Start implements environment.Launcher.
func (h *handle) Start(argv []string, sink io.Writer) (environment.Process, error) {
	if len(argv) == 0 || argv[0] == "" {
		return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: "dockercli: no command to run"}
	}
	args := append([]string{"exec", h.name, "/bin/sh", "-c", execWrapper, "vs-exec"}, argv...)
	cmd := exec.Command(h.e.program, args...)
	var stderr lockedBuffer
	cmd.Stdout, cmd.Stderr = sink, &stderr

	h.mu.Lock()
	defer h.mu.Unlock()
	if h.closed {
		return nil, unavailable("the container %s is closed", h.name)
	}
	if err := cmd.Start(); err != nil {
		return nil, unavailable("cannot run %q: %v; %s", h.e.program, err, hint)
	}
	p := &proc{cmd: cmd, exited: make(chan environment.Exit, 1)}
	h.procs = append(h.procs, p)
	h.wg.Go(func() { p.exited <- h.finish(cmd.Wait(), stderr.String()) })
	return p, nil
}

// finish says how a command ended, from how its docker client did.
func (h *handle) finish(waitErr error, stderr string) environment.Exit {
	var exit *exec.ExitError
	code := 0
	switch {
	case waitErr == nil:
	case errors.As(waitErr, &exit):
		code = exit.ExitCode()
	default:
		return environment.Exit{Err: unavailable("waiting for a command of %s: %v", h.name, waitErr)}
	}
	if code == 0 {
		return environment.Exit{}
	}
	if code < 0 {
		// The client was killed, by Kill or by Close. What its command went on to do is not
		// known: it runs on until the container is removed.
		return environment.Exit{Kind: core.ExecUnknown}
	}
	// The command's own standard error is merged into its output inside the container, so what
	// the client writes to standard error is the engine's: it could not reach the container.
	if strings.TrimSpace(stderr) != "" {
		return environment.Exit{Kind: core.ExecUnknown}
	}
	// A command killed by a signal and a command whose container was removed under it both exit
	// with 128 plus the signal. Only a container that still runs says it was the command.
	if code >= 128 && !h.running() {
		return environment.Exit{Kind: core.ExecUnknown}
	}
	return environment.Exit{Code: code}
}

// running reports whether the container exists and runs. An engine that cannot say is not
// believed to run it.
func (h *handle) running() bool {
	res, err := h.e.call(context.Background(), "inspect", "inspect", "--format", "{{.State.Running}}", h.name)
	return err == nil && res.code == 0 && strings.TrimSpace(res.stdout) == "true"
}

// Close implements docker.Container. It closes the container's input, which stops the container
// when the caller has not removed it, ends the clients of the commands, and waits for everything
// this handle started. It does not remove the container.
func (h *handle) Close() {
	h.once.Do(func() {
		h.mu.Lock()
		h.closed = true
		procs := slices.Clone(h.procs)
		h.mu.Unlock()
		h.stdin.Close()
		for _, p := range procs {
			p.Kill()
		}
		fired, stop := h.e.timers.Timeout(h.e.timeout)
		select {
		case <-h.keeperDone:
		case <-fired:
			// The container did not stop with its input: end the client that holds it.
			_ = h.keeper.Process.Kill()
			<-h.keeperDone
		}
		stop()
		h.wg.Wait()
	})
}

// lockedBuffer is a buffer that exec's goroutines and a reader can share.
type lockedBuffer struct {
	mu  sync.Mutex
	buf strings.Builder
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.buf.Len() < 64<<10 {
		b.buf.Write(p)
	}
	return len(p), nil
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}
