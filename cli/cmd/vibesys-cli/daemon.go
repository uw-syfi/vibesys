package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/production"
)

// daemonCommand is `daemon status|ensure|stop|restart|forget`, and `daemon run`, which is the daemon
// process itself and is what a client starts.
func daemonCommand(ctx context.Context, d deps, args []string) int {
	if len(args) == 0 {
		return fail(d, d.jsonErrors, usagef("daemon: status, ensure, stop, restart or forget"))
	}
	switch args[0] {
	case "status":
		return daemonStatus(ctx, d, args[1:])
	case "ensure":
		return daemonEnsure(ctx, d, args[1:])
	case "stop":
		return daemonStop(ctx, d, args[1:])
	case "restart":
		return daemonRestart(ctx, d, args[1:])
	case "forget":
		return daemonForget(ctx, d, args[1:])
	case "run":
		return daemonRun(ctx, d)
	case "install":
		return fail(d, d.jsonErrors, usagef("daemon install is not available in this build; the daemon starts when a command needs it"))
	default:
		return fail(d, d.jsonErrors, usagef("daemon: %q is not a daemon command (status, ensure, stop, restart or forget)", args[0]))
	}
}

// dialExisting reaches the daemon that runs and starts none.
func dialExisting(ctx context.Context, d deps) (*client.Client, error) {
	opts, err := d.options()
	if err != nil {
		return nil, err
	}
	return client.Dial(ctx, opts)
}

func daemonStatus(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("daemon status", d)
	if _, err := parse(fs, args); err != nil {
		return parseFailed(d, fs, err)
	}
	cl, err := dialExisting(ctx, d)
	if err == client.ErrNoDaemon {
		// No daemon is no answer, and with --json it is the Failure that says so.
		if c.json {
			return fail(d, true, err)
		}
		fmt.Fprintln(d.stdout, "not running")
		return exitNoAnswer
	}
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	st, err := cl.Status(ctx)
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, st)
		return exitOK
	}
	fmt.Fprintf(d.stdout, "running\tpid %d\tversion %s\tqueued %d\trunning %d\n",
		st.GetPid(), st.GetVersions().GetBinaryVersion(), st.GetQueuedJobs(), st.GetRunningJobs())
	// A sandbox daemon says so, and every daemon names the sandbox daemons that run beside the one
	// that holds the machine, so none runs unseen.
	if st.GetSandbox() {
		fmt.Fprintln(d.stdout, "role\tsandbox")
	}
	for _, sb := range st.GetSandboxes() {
		fmt.Fprintf(d.stdout, "sandbox\tpid %d\truntime %s\tversion %s\n", sb.GetPid(), sb.GetRuntimeDir(), sb.GetVersion())
	}
	// What a job inherits: the checkout directory, then one line per variable, with its value
	// only where the daemon shows it.
	if dir := st.GetCheckoutDirectory(); dir != "" {
		fmt.Fprintf(d.stdout, "checkouts\t%s\n", dir)
	}
	for _, v := range st.GetEnvironment() {
		if v.Value != nil {
			fmt.Fprintf(d.stdout, "env\t%s=%s\n", v.GetName(), v.GetValue())
		} else {
			fmt.Fprintf(d.stdout, "env\t%s\n", v.GetName())
		}
	}
	return exitOK
}

// daemonEnsure is the one way to have a daemon that this build can use. It does what every
// command does to reach the daemon: it takes the right to start, and starts a daemon when none
// runs or replaces one older than this build, and waits until it answers. A caller that is not a
// command line, such as the orchestrator, runs this and reads the exit code only: 0 means a
// compatible daemon answers, and 4 means none does, whatever the reason; the message names it.
// Only a command that is itself malformed ends otherwise, with 2.
func daemonEnsure(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("daemon ensure", d)
	if _, err := parse(fs, args); err != nil {
		return parseFailed(d, fs, err)
	}
	opts, err := d.options()
	if err != nil {
		fail(d, c.json, err)
		return exitNoAnswer
	}
	st, err := client.Ensure(ctx, opts)
	if err == nil {
		if c.json {
			printJSON(d, st)
		} else {
			fmt.Fprintf(d.stdout, "ready\tpid %d\tversion %s\n", st.GetPid(), st.GetVersions().GetBinaryVersion())
		}
		return exitOK
	}
	fail(d, c.json, err)
	return exitNoAnswer
}

// daemonStop stops the daemon. Jobs that run have supervisors of their own and go on. Stopping a
// daemon that does not run is not an error.
func daemonStop(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("daemon stop", d)
	if _, err := parse(fs, args); err != nil {
		return parseFailed(d, fs, err)
	}
	cl, err := dialExisting(ctx, d)
	if err == client.ErrNoDaemon {
		if c.json {
			printJSON(d, &cliv1.StopResponse{})
		} else {
			fmt.Fprintln(d.stdout, "not running")
		}
		return exitOK
	}
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	if err := cl.Stop(ctx); err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, &cliv1.StopResponse{})
	} else {
		fmt.Fprintln(d.stdout, "stopped")
	}
	return exitOK
}

func daemonRestart(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("daemon restart", d)
	if _, err := parse(fs, args); err != nil {
		return parseFailed(d, fs, err)
	}
	opts, err := d.options()
	if err != nil {
		return fail(d, c.json, err)
	}
	cl, err := client.Restart(ctx, opts)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	st, err := cl.Status(ctx)
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, st)
		return exitOK
	}
	fmt.Fprintf(d.stdout, "restarted\tpid %d\tversion %s\n", st.GetPid(), st.GetVersions().GetBinaryVersion())
	return exitOK
}

// daemonForget gives up a repository that is gone for good, so that nothing waits for it.
func daemonForget(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("daemon forget", d)
	rest, err := parse(fs, args)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	if len(rest) != 1 {
		return fail(d, c.json, usagef("daemon forget: name one repository: daemon forget <path>"))
	}
	repository, err := filepath.Abs(rest[0])
	if err != nil {
		return fail(d, c.json, usagef("daemon forget: %v", err))
	}
	cl, err := dialExisting(ctx, d)
	if err == client.ErrNoDaemon {
		return fail(d, c.json, &unavailable{"the daemon is not running; a repository is forgotten by the daemon that knows it"})
	}
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	res, err := cl.ForgetRepository(ctx, repository)
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, res)
		return exitOK
	}
	fmt.Fprintf(d.stdout, "forgot\t%s\ttasks %s\n", repository, strings.Join(res.GetTasks(), ","))
	for _, job := range res.GetLeft() {
		fmt.Fprintf(d.stdout, "left\t%s\tits supervisor may still run; it clears when that ends or the machine restarts\n", job)
	}
	return exitOK
}

// daemonRun is the daemon process. A client starts it with the readiness pipe on its descriptor;
// started by hand there is none, and it is simply a daemon in the foreground.
func daemonRun(ctx context.Context, d deps) int {
	if err := d.process.RunDaemon(ctx, readyPipe()); err != nil {
		if errors.Is(err, production.ErrAlreadyRunning) {
			return exitOK
		}
		fmt.Fprintf(d.stderr, "vibesys-cli: %v\n", err)
		return exitFailed
	}
	return exitOK
}

// readyPipe is the readiness pipe a parent passed, or nil when the process was not started with one.
// It returns the interface, as a nil *os.File in an io.WriteCloser would not be nil to the daemon.
func readyPipe() io.WriteCloser {
	f := os.NewFile(uintptr(production.ReadyFD), "ready")
	if f == nil {
		return nil
	}
	// Only a pipe is that pipe. The descriptor may not be open, and it may be open and something
	// else, such as the Go runtime's own epoll descriptor, and writing to it would stop the daemon.
	if info, err := f.Stat(); err != nil || info.Mode()&fs.ModeNamedPipe == 0 {
		return nil
	}
	return f
}

// supervisorCommand is `supervisor serve --run-dir dir`, the process the daemon starts for a job.
func supervisorCommand(ctx context.Context, d deps, args []string) int {
	if len(args) == 0 || args[0] != "serve" {
		return fail(d, d.jsonErrors, usagef("supervisor: serve --run-dir <dir> (the daemon runs this; it is not for hands)"))
	}
	fs, _ := newFlags("supervisor serve", d)
	runDir := fs.String("run-dir", "", "the job's run directory")
	if _, err := parse(fs, args[1:]); err != nil {
		return parseFailed(d, fs, err)
	}
	ready := readyPipe()
	if ready == nil {
		return fail(d, d.jsonErrors, usagef("supervisor serve: the daemon passes its readiness pipe on descriptor %d", production.ReadyFD))
	}
	if err := d.process.RunSupervisor(ctx, *runDir, ready); err != nil {
		fmt.Fprintf(d.stderr, "vibesys-cli: %v\n", err)
		return exitFailed
	}
	return exitOK
}
