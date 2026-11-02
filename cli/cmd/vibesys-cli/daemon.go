package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/production"
)

// daemonCommand is `daemon status|stop|restart`, and `daemon run`, which is the daemon process
// itself and is what a client starts.
func daemonCommand(ctx context.Context, d deps, args []string) int {
	if len(args) == 0 {
		return fail(d, d.jsonErrors, usagef("daemon: status, stop or restart"))
	}
	switch args[0] {
	case "status":
		return daemonStatus(ctx, d, args[1:])
	case "stop":
		return daemonStop(ctx, d, args[1:])
	case "restart":
		return daemonRestart(ctx, d, args[1:])
	case "run":
		return daemonRun(ctx, d)
	case "install":
		return fail(d, d.jsonErrors, &unavailable{"daemon install is not available in this build; the daemon starts when a command needs it"})
	default:
		return fail(d, d.jsonErrors, usagef("daemon: %q is not a daemon command (status, stop or restart)", args[0]))
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
		fmt.Fprintln(d.stdout, "not running")
		return exitFailed
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
	return exitOK
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
