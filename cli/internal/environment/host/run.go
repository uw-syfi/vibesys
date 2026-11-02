//go:build unix

package host

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// guard is the shell program every leader is started by. It starts a watcher that waits for the
// end of the file on descriptor 3, a pipe whose write end only the caller of Run holds, and kills
// the leader's whole process group when it comes: the caller closes it at the end of Run, and
// the kernel closes it if the caller dies. Then the shell becomes the command, which does not
// inherit the descriptor.
//
// The leader's process id is the group's id, since the leader starts a group of its own, and the
// shell becomes the command without changing its id.
const guard = `( read -r _ <&3; kill -s KILL -- "-$$" ) &
exec "$@" 3<&-`

// proc is one process group: a leader and what it started.
type proc struct {
	pgid   int
	cmd    *exec.Cmd
	exited chan environment.Exit // receives once, when the leader has ended and been reaped
	copied chan struct{}         // closed when everything the group wrote has been delivered
	read   *os.File              // the read end of the group's output pipe
}

// run is one call of Run: the processes it started and what it needs to end them.
type run struct {
	ctx   context.Context
	env   *Environment
	x     core.Execution
	sink  *lockedWriter
	limit <-chan struct{}

	stopLimit func()
	lifeRead  *os.File // the guards' end of the lifeline
	lifeWrite *os.File // the caller's end: closing it, or dying, ends every group
	procs     []*proc
	wg        sync.WaitGroup
}

func newRun(ctx context.Context, e *Environment, x core.Execution, out io.Writer) (*run, error) {
	r := &run{ctx: ctx, env: e, x: x, sink: &lockedWriter{w: out}}
	// The limit comes first, before anything starts, so that it bounds everything below.
	r.limit, r.stopLimit = e.timers.Timeout(x.Timeout)
	var err error
	if r.lifeRead, r.lifeWrite, err = os.Pipe(); err != nil {
		r.stopLimit()
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("host: cannot make the pipe that guards the run: %v", err)}
	}
	return r, nil
}

// execute decides how the run ends: environment.Execute does, over the processes this run starts.
func (r *run) execute() (core.ExecOutcome, error) {
	return environment.Execute(r.x, r, environment.Events{
		Limit:     r.limit,
		Cancelled: r.ctx.Done(),
		Pause:     func() (<-chan struct{}, func()) { return r.env.timers.Timeout(r.env.pause) },
	}, r.sink)
}

// start starts argv as the leader of a new process group in the checkout, with its combined
// output going to sink. The command must exist: one that cannot be started is an error, not an
// outcome.
func (r *run) Start(argv []string, sink io.Writer) (environment.Process, error) {
	dir := r.x.Checkout.Dir
	path, err := resolve(argv[0], dir)
	if err != nil {
		return nil, err
	}
	args := append([]string{"-c", guard, "vs-host", path}, argv[1:]...)
	cmd := exec.Command("/bin/sh", args...)
	cmd.Dir = dir
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.ExtraFiles = []*os.File{r.lifeRead}
	pr, pw, err := os.Pipe()
	if err != nil {
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("host: cannot make the output pipe: %v", err)}
	}
	cmd.Stdout, cmd.Stderr = pw, pw
	if err := cmd.Start(); err != nil {
		pr.Close()
		pw.Close()
		return nil, &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("host: cannot start %q in %s: %v", argv[0], dir, err)}
	}
	pw.Close()
	p := &proc{
		pgid: cmd.Process.Pid, cmd: cmd, read: pr,
		exited: make(chan environment.Exit, 1), copied: make(chan struct{}),
	}
	r.procs = append(r.procs, p)
	r.wg.Go(func() { p.exited <- wait(cmd) })
	r.wg.Go(func() {
		defer close(p.copied)
		_, _ = io.Copy(sink, pr)
	})
	return p, nil
}

// resolve finds the file argv[0] names, as the shell would run it from dir.
func resolve(name, dir string) (string, error) {
	notFound := func(format string, args ...any) error {
		return &core.Failure{Kind: core.FailureNotFound, Message: "host: " + fmt.Sprintf(format, args...)}
	}
	if strings.Contains(name, "/") {
		path := name
		if !filepath.IsAbs(path) {
			path = filepath.Join(dir, path)
		}
		info, err := os.Stat(path)
		if err != nil || info.IsDir() || info.Mode()&0o111 == 0 {
			return "", notFound("cannot start %q: it is not an executable file", name)
		}
		return path, nil
	}
	path, err := exec.LookPath(name)
	if err != nil {
		return "", notFound("cannot start %q: %v", name, err)
	}
	return path, nil
}

// wait reaps the leader and says how it ended. A signal ends a process with 128 plus its number,
// as a shell reports it.
func wait(cmd *exec.Cmd) environment.Exit {
	_ = cmd.Wait()
	state := cmd.ProcessState
	if state == nil {
		return environment.Exit{Err: &core.Failure{Kind: core.FailureInternal, Message: "host: the command was not waited for"}}
	}
	status, ok := state.Sys().(syscall.WaitStatus)
	if !ok {
		return environment.Exit{Code: state.ExitCode()}
	}
	if status.Signaled() {
		return environment.Exit{Code: 128 + int(status.Signal())}
	}
	return environment.Exit{Code: status.ExitStatus()}
}

// Exited implements environment.Process.
func (p *proc) Exited() <-chan environment.Exit { return p.exited }

// Kill implements environment.Process: it ends the group, whatever is left of it.
func (p *proc) Kill() {
	// The group no longer existing is the usual answer for a command that ended.
	_ = syscall.Kill(-p.pgid, syscall.SIGKILL)
}

// end ends every group, waits for every goroutine and releases the descriptors. It reports the
// failure of the sink, which is the one thing the run cannot recover from: output was lost.
func (r *run) end() error {
	for _, p := range r.procs {
		p.Kill()
	}
	for _, p := range r.procs {
		select {
		case <-p.copied:
		case <-r.limit:
			// The limit bounds the whole execution, drain included: a process that left the
			// group and keeps the output open must not hold the run for ever.
			p.read.Close()
		}
	}
	r.wg.Wait()
	for _, p := range r.procs {
		p.read.Close()
	}
	r.lifeWrite.Close()
	r.lifeRead.Close()
	r.stopLimit()
	if err := r.sink.failure(); err != nil {
		return &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("host: the output could not be kept: %v", err)}
	}
	return nil
}
