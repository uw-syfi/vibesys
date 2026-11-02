package environment

import (
	"context"
	"fmt"
	"io"
	"path"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Validate refuses an execution no environment can run, with a *core.Failure of kind
// FailureInvalidInput that names what is wrong. Every implementation calls it first, so that the
// refusal does not depend on which environment is asked.
func Validate(x core.Execution) error {
	invalid := func(format string, args ...any) error {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: "execution: " + fmt.Sprintf(format, args...)}
	}
	switch {
	case len(x.Argv) == 0 || x.Argv[0] == "":
		return invalid("no command to run")
	case x.Timeout <= 0:
		return invalid("the timeout must be positive, got %v", x.Timeout)
	case x.Checkout.Dir == "" || x.Checkout.Dir[0] != '/':
		return invalid("the checkout directory must be an absolute path, got %q", x.Checkout.Dir)
	case x.Scratch != "" && x.Scratch[0] != '/':
		return invalid("the scratch directory must be an absolute path, got %q", x.Scratch)
	case x.Scratch != "" && overlap(x.Checkout.Dir, x.Scratch):
		return invalid("the scratch directory %q and the checkout %q must be apart: neither may be inside the other", x.Scratch, x.Checkout.Dir)
	}
	if s := x.Service; s != nil {
		if len(s.Argv) == 0 || s.Argv[0] == "" {
			return invalid("the service has no command to run")
		}
		if len(s.Probe) > 0 && s.Probe[0] == "" {
			return invalid("the service's probe has an empty program name")
		}
	}
	return nil
}

// overlap reports whether one absolute directory is the other or inside it.
func overlap(a, b string) bool {
	a, b = path.Clean(a), path.Clean(b)
	return a == b || strings.HasPrefix(a, strings.TrimSuffix(b, "/")+"/") || strings.HasPrefix(b, strings.TrimSuffix(a, "/")+"/")
}

// Exit is how a process group's leader ended.
type Exit struct {
	Code int
	// Kind, when set, says the leader ended in a way that is not an exit status, such as a fake's
	// scripted timeout: the execution then ends with this kind.
	Kind core.ExecKind
	// Err is set when the leader could not be waited for, such as a container the environment
	// lost: how it ended is then not known.
	Err error
}

// Process is a started process group.
type Process interface {
	// Exited receives once, when the group's leader has ended.
	Exited() <-chan Exit
	// Kill ends the whole group.
	Kill()
}

// Launcher starts the processes of an execution. It is the I/O of an environment: a host starts
// shell processes, a fake interprets scripts, and a docker environment execs in a container.
type Launcher interface {
	// Start starts argv as the leader of a new process group, with its combined output going to
	// sink. A command that cannot be started is an error, not an exit, and nothing of it ran.
	Start(argv []string, sink io.Writer) (Process, error)
}

// Events are the things an execution waits for besides its processes.
type Events struct {
	// Limit is closed when the execution's Timeout is reached.
	Limit <-chan struct{}
	// Cancelled is closed when the caller cancels the execution.
	Cancelled <-chan struct{}
	// Pause returns a channel closed after the pause between two readiness probes, and the
	// function that releases it.
	Pause func() (fired <-chan struct{}, stop func())
}

// Execute decides how an execution ends from what its processes and the events do: it starts the
// service, probes it until it is ready, runs the command, and reports the first thing that ends
// it. The limit bounds everything, so a service that is not ready at the limit is
// ExecServiceNotReady and a command that is running at the limit is ExecTimedOut; the service
// ending while the command runs is ExecServiceExited unless the command ended too.
//
// A process that could not be started before any other was is core.NotStarted. Once one has
// started, a failure to start the next or to wait for one is core.Unobserved: what ran may have
// had effects.
//
// It starts processes through the launcher and ends none of them: ending the groups, and
// everything else that must hold when the execution is over, is the caller's, on every path.
func Execute(x core.Execution, l Launcher, ev Events, out io.Writer) core.Attempt {
	started := false
	failed := func(err error) core.Attempt {
		if started {
			return core.Unobserved{Failure: core.AsFailure(err)}
		}
		return core.NotStarted{Failure: core.AsFailure(err)}
	}
	var serviceExited <-chan Exit
	if s := x.Service; s != nil {
		svc, err := l.Start(s.Argv, out)
		if err != nil {
			return failed(err)
		}
		started = true
		serviceExited = svc.Exited()
		if len(s.Probe) > 0 {
			if ended, err := awaitReady(l, s.Probe, svc, ev); err != nil {
				return failed(err)
			} else if ended != nil {
				return core.Ended{Exec: *ended}
			}
		}
	}
	main, err := l.Start(x.Argv, out)
	if err != nil {
		return failed(err)
	}
	started = true
	select {
	case st := <-main.Exited():
		return exited(st)
	case <-serviceExited:
		// The command may have ended at the same time: a finished command is the result.
		select {
		case st := <-main.Exited():
			return exited(st)
		default:
			return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecServiceExited}}
		}
	case <-ev.Limit:
		return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecTimedOut}}
	case <-ev.Cancelled:
		return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	}
}

// exited is the end of a command whose leader was seen to end, or could not be waited for.
func exited(st Exit) core.Attempt {
	if st.Err != nil {
		return core.Unobserved{Failure: core.AsFailure(st.Err)}
	}
	if st.Kind != "" {
		return core.Ended{Exec: core.ExecOutcome{Kind: st.Kind}}
	}
	return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecExited, ExitCode: st.Code}}
}

// awaitReady probes the service until a probe succeeds. It returns nil when the service is ready,
// the ending that ended the wait, or an error when a probe could not be run or waited for.
func awaitReady(l Launcher, probe []string, svc Process, ev Events) (*core.ExecOutcome, error) {
	end := func(kind core.ExecKind) (*core.ExecOutcome, error) { return &core.ExecOutcome{Kind: kind}, nil }
	for {
		p, err := l.Start(probe, io.Discard)
		if err != nil {
			return nil, err
		}
		select {
		case st := <-p.Exited():
			p.Kill()
			if st.Err != nil {
				return nil, st.Err
			}
			if st.Code == 0 && st.Kind == "" {
				return nil, nil
			}
		case <-svc.Exited():
			p.Kill()
			return end(core.ExecServiceExited)
		case <-ev.Limit:
			p.Kill()
			return end(core.ExecServiceNotReady)
		case <-ev.Cancelled:
			p.Kill()
			return end(core.ExecCancelled)
		}
		pause, stop := ev.Pause()
		select {
		case <-pause:
			stop()
		case <-svc.Exited():
			stop()
			return end(core.ExecServiceExited)
		case <-ev.Limit:
			stop()
			return end(core.ExecServiceNotReady)
		case <-ev.Cancelled:
			stop()
			return end(core.ExecCancelled)
		}
	}
}

// Finish is the last word on an attempt. One that lost something on the way out, said by lost,
// keeps its failure and says what was lost, and an Ended one becomes Unobserved: nobody can vouch
// for how it ended. Otherwise an attempt that failed while ctx was being cancelled was ended by the
// cancel. Every environment ends its Run with it, so the rule does not depend
// on which environment ran the command.
func Finish(ctx context.Context, a core.Attempt, lost error) core.Attempt {
	switch a := a.(type) {
	case core.NotStarted:
		if lost != nil {
			// Nothing ran, so what was not cleaned up held nothing of the command.
			a.Failure.Message += "; and " + lost.Error()
			return a
		}
		if ctx.Err() != nil {
			return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
		}
		return a
	case core.Unobserved:
		if lost != nil {
			a.Failure.Message += "; and " + lost.Error()
			return a
		}
		if ctx.Err() != nil {
			return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
		}
		return a
	case core.Ended:
		if lost != nil {
			return core.Unobserved{Failure: core.AsFailure(lost)}
		}
		return a
	}
	return core.Unobserved{Failure: core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("the environment ended with %T", a)}}
}
