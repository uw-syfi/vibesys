package environment

import (
	"fmt"
	"io"

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

// Exit is how a process group's leader ended.
type Exit struct {
	Code int
	// Kind, when set, says the leader ended in a way that is not an exit status, such as a
	// container the environment lost: the execution then ends with this kind.
	Kind core.ExecKind
	// Err is set when the leader could not be waited for.
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
// shell processes, a fake interprets scripts, and a docker environment would start containers.
type Launcher interface {
	// Start starts argv as the leader of a new process group, with its combined output going to
	// sink. A command that cannot be started is an error, not an exit.
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
// It starts processes through the launcher and ends none of them: ending the groups, and
// everything else that must hold when the execution is over, is the caller's, on every path.
func Execute(x core.Execution, l Launcher, ev Events, out io.Writer) (core.ExecOutcome, error) {
	var serviceExited <-chan Exit
	if s := x.Service; s != nil {
		svc, err := l.Start(s.Argv, out)
		if err != nil {
			return core.ExecOutcome{}, err
		}
		serviceExited = svc.Exited()
		if len(s.Probe) > 0 {
			if outcome, ready, err := awaitReady(l, s.Probe, svc, ev); err != nil || !ready {
				return outcome, err
			}
		}
	}
	main, err := l.Start(x.Argv, out)
	if err != nil {
		return core.ExecOutcome{}, err
	}
	select {
	case st := <-main.Exited():
		return exited(st)
	case <-serviceExited:
		// The command may have ended at the same time: a finished command is the result.
		select {
		case st := <-main.Exited():
			return exited(st)
		default:
			return core.ExecOutcome{Kind: core.ExecServiceExited}, nil
		}
	case <-ev.Limit:
		return core.ExecOutcome{Kind: core.ExecTimedOut}, nil
	case <-ev.Cancelled:
		return core.ExecOutcome{Kind: core.ExecCancelled}, nil
	}
}

func exited(st Exit) (core.ExecOutcome, error) {
	if st.Err != nil {
		return core.ExecOutcome{}, st.Err
	}
	if st.Kind != "" {
		return core.ExecOutcome{Kind: st.Kind}, nil
	}
	return core.ExecOutcome{Kind: core.ExecExited, ExitCode: st.Code}, nil
}

// awaitReady probes the service until a probe succeeds. It returns ready, or the outcome that
// ended the wait.
func awaitReady(l Launcher, probe []string, svc Process, ev Events) (outcome core.ExecOutcome, ready bool, err error) {
	for {
		p, err := l.Start(probe, io.Discard)
		if err != nil {
			return core.ExecOutcome{}, false, err
		}
		select {
		case st := <-p.Exited():
			p.Kill()
			if st.Err != nil {
				return core.ExecOutcome{}, false, st.Err
			}
			if st.Code == 0 && st.Kind == "" {
				return core.ExecOutcome{}, true, nil
			}
		case <-svc.Exited():
			p.Kill()
			return core.ExecOutcome{Kind: core.ExecServiceExited}, false, nil
		case <-ev.Limit:
			p.Kill()
			return core.ExecOutcome{Kind: core.ExecServiceNotReady}, false, nil
		case <-ev.Cancelled:
			p.Kill()
			return core.ExecOutcome{Kind: core.ExecCancelled}, false, nil
		}
		pause, stop := ev.Pause()
		select {
		case <-pause:
			stop()
		case <-svc.Exited():
			stop()
			return core.ExecOutcome{Kind: core.ExecServiceExited}, false, nil
		case <-ev.Limit:
			stop()
			return core.ExecOutcome{Kind: core.ExecServiceNotReady}, false, nil
		case <-ev.Cancelled:
			stop()
			return core.ExecOutcome{Kind: core.ExecCancelled}, false, nil
		}
	}
}
