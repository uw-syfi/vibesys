package environment

import (
	"context"
	"fmt"
	"io"
	"path"
	"strconv"
	"strings"
	"time"

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
	case x.Artifacts != "" && x.Artifacts[0] != '/':
		return invalid("the artifacts directory must be an absolute path, got %q", x.Artifacts)
	case x.Artifacts != "" && (overlap(x.Checkout.Dir, x.Artifacts) || (x.Scratch != "" && overlap(x.Scratch, x.Artifacts))):
		return invalid("the artifacts directory %q must be apart from the checkout and the scratch directory", x.Artifacts)
	}
	if s := x.Service; s != nil {
		if len(s.Argv) == 0 || s.Argv[0] == "" {
			return invalid("the service has no command to run")
		}
		if len(s.Probe) > 0 && s.Probe[0] == "" {
			return invalid("the service's probe has an empty program name")
		}
	}
	if c := x.Candidate; c != nil {
		if !core.ValidImageDigest(c.Image) {
			return invalid("the candidate's image %q is not an exact digest (sha256:<64 hex digits>)", c.Image)
		}
		if s := c.Serve; s != nil {
			switch {
			case x.Service != nil:
				return invalid("the command runs against the candidate's server, and has no service of its own")
			case len(s.Argv) == 0 || s.Argv[0] == "":
				return invalid("the candidate's server has no command to run")
			case !strings.HasPrefix(s.Ready, "/") || strings.ContainsAny(s.Ready, " \t\n"):
				return invalid("the candidate's readiness path %q is not an absolute HTTP path", s.Ready)
			case s.StartupTimeout <= 0:
				return invalid("the candidate's startup time must be positive, got %v", s.StartupTimeout)
			}
		}
	}
	return nil
}

// ValidateService refuses a job-scoped server no environment can start, with a *core.Failure of
// kind FailureInvalidInput that names what is wrong, as Validate does for an execution.
func ValidateService(s core.JobService) error {
	invalid := func(format string, args ...any) error {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: "service: " + fmt.Sprintf(format, args...)}
	}
	switch {
	case len(s.Argv) == 0 || s.Argv[0] == "":
		return invalid("no command to run")
	case s.Checkout.Dir == "" || s.Checkout.Dir[0] != '/':
		return invalid("the checkout directory must be an absolute path, got %q", s.Checkout.Dir)
	case s.Artifacts != "" && (s.Artifacts[0] != '/' || overlap(s.Checkout.Dir, s.Artifacts)):
		return invalid("the artifacts directory %q must be an absolute path apart from the checkout", s.Artifacts)
	case !strings.HasPrefix(s.Ready, "/") || strings.ContainsAny(s.Ready, " \t\n"):
		return invalid("the readiness path %q is not an absolute HTTP path", s.Ready)
	case s.StartupTimeout <= 0:
		return invalid("the startup time must be positive, got %v", s.StartupTimeout)
	case s.Lifetime <= 0:
		return invalid("the lifetime must be positive, got %v", s.Lifetime)
	}
	return nil
}

// ValidateAgainst refuses an execution that cannot run against a job-scoped server: one that is
// not valid, or names a service or a candidate of its own.
func ValidateAgainst(x core.Execution) error {
	if err := Validate(x); err != nil {
		return err
	}
	if x.Service != nil || x.Candidate != nil {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: "execution: a command that runs against the job's server has no service and no candidate of its own"}
	}
	return nil
}

// ResolvePort is the candidate's server command with the port it is to listen on.
func ResolvePort(argv []string, port int) []string {
	out := make([]string, len(argv))
	for i, a := range argv {
		out[i] = strings.ReplaceAll(a, core.PortPlaceholder, strconv.Itoa(port))
	}
	return out
}

// ServiceURL is where the commands of the task's image reach the candidate's server.
func ServiceURL(port int) string { return fmt.Sprintf("http://127.0.0.1:%d", port) }

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
	// Terminate asks the whole group to stop, with SIGTERM, and returns at once. A command that
	// traps it can clean up what it holds outside the machine; one that ignores it is ended by
	// Kill. It is a request and no end: only Kill ends the group.
	Terminate()
	// Kill ends the whole group, at once, whatever is left of it.
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
	// Startup, when set, is closed when the service has had its time to become ready: a service
	// not ready by then is ExecServiceNotReady, as at the limit. Nil gives it until the limit.
	Startup <-chan struct{}
	// Cancelled is closed when the caller cancels the execution.
	Cancelled <-chan struct{}
	// ServiceGone, for an execution that runs against a server started before it (Service.Run),
	// is closed when that server has ended: the execution then ends with ExecServiceExited, as it
	// does when its own service ends. Nil names no such server.
	ServiceGone <-chan struct{}
	// Pause returns a channel closed after the pause between two readiness probes, and the
	// function that releases it.
	Pause func() (fired <-chan struct{}, stop func())
	// Grace returns a channel closed when the grace period ends, and the function that releases
	// it. It is armed when the limit is reached or the caller cancels, after the processes were
	// asked to terminate. Nil gives no grace: the caller kills the groups at once.
	Grace func() (fired <-chan struct{}, stop func())
}

// DefaultGrace is how long the commands of an execution get to clean up after they are asked to
// terminate, when the limit is reached or the caller cancels, before they are killed.
const DefaultGrace = 30 * time.Second

// terminate asks the processes to stop and waits until their leaders have exited or the grace
// period is over, whichever is first. It ends nothing: the caller kills what is left. Leaders
// that exited have their exit consumed, which no one reads after the execution is decided.
func (ev Events) terminate(procs ...Process) {
	if ev.Grace == nil {
		return
	}
	for _, p := range procs {
		p.Terminate()
	}
	fired, stop := ev.Grace()
	defer stop()
	for _, p := range procs {
		select {
		case <-p.Exited():
		case <-fired:
			return
		}
	}
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
	return ExecuteBeside(x, Places{Command: l, Service: l}, ev, out)
}

// Places are where an execution's processes start: the command and the service's probe in one
// place, and the service in another, or in the same.
type Places struct {
	Command Launcher
	Service Launcher
}

// ExecuteBeside is Execute with the service started in a place of its own, as the candidate's
// server of a task of two images is, in its own container beside the command's. For such an
// execution (one whose Candidate serves) the service is asked to terminate, and given the grace
// period to end, on every path the command ended by, so that a server under a profiler writes what
// it gathered before its container is removed.
func ExecuteBeside(x core.Execution, places Places, ev Events, out io.Writer) core.Attempt {
	l := places.Command
	started := false
	failed := func(err error) core.Attempt {
		if started {
			return core.Unobserved{Failure: core.AsFailure(err)}
		}
		return core.NotStarted{Failure: core.AsFailure(err)}
	}
	var serviceExited <-chan Exit
	var running []Process // the groups a limit or a cancel asks to terminate
	var svc Process
	if s := x.Service; s != nil {
		var ended core.Attempt
		if svc, ended = StartService(*s, places, ev, out); ended != nil {
			return ended
		}
		started = true
		running = append(running, svc)
		serviceExited = svc.Exited()
	}
	main, err := l.Start(x.Argv, out)
	if err != nil {
		return failed(err)
	}
	started = true
	running = append([]Process{main}, running...)
	// A candidate's server is ended gently once the command has ended, however it ended.
	gentle := func(a core.Attempt) core.Attempt {
		if svc != nil && x.Candidate != nil && x.Candidate.Serve != nil {
			ev.terminate(svc)
		}
		return a
	}
	select {
	case st := <-main.Exited():
		return gentle(exited(st))
	case <-serviceExited:
		// The command may have ended at the same time: a finished command is the result.
		select {
		case st := <-main.Exited():
			return exited(st)
		default:
			return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecServiceExited}}
		}
	case <-ev.ServiceGone:
		// The same for the server the command runs against, which the command does not own: it is
		// not asked to terminate, since the server is already gone.
		select {
		case st := <-main.Exited():
			return exited(st)
		default:
			return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecServiceExited}}
		}
	case <-ev.Limit:
		ev.terminate(running...)
		return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecTimedOut}}
	case <-ev.Cancelled:
		ev.terminate(running...)
		return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	}
}

// StartService starts a service in places.Service and probes it from places.Command until it is
// ready, and returns it running with a nil attempt. Otherwise it returns a nil process and how the
// start ended: core.NotStarted when the service could not be started, core.Ended with
// ExecServiceNotReady, ExecServiceExited or ExecCancelled when the events ended the wait (after
// asking the service to terminate, given the grace), and core.Unobserved when a probe could not be
// run or waited for. A service without a probe is ready at once. It ends nothing: ending the group
// is the caller's, on every path.
func StartService(s core.ServiceSpec, places Places, ev Events, out io.Writer) (Process, core.Attempt) {
	svc, err := places.Service.Start(s.Argv, out)
	if err != nil {
		return nil, core.NotStarted{Failure: core.AsFailure(err)}
	}
	if len(s.Probe) > 0 {
		if ended, err := awaitReady(places.Command, s.Probe, svc, ev); err != nil {
			return nil, core.Unobserved{Failure: core.AsFailure(err)}
		} else if ended != nil {
			return nil, core.Ended{Exec: *ended}
		}
	}
	return svc, nil
}

// Watched is a running service whose end is seen once, by a goroutine of its own, so that many may
// ask whether it ended: a Process's Exited receives only once.
type Watched struct {
	p    Process
	done chan struct{}
}

// Watch starts watching the process, which nothing else may receive from Exited afterwards.
func Watch(p Process) *Watched {
	w := &Watched{p: p, done: make(chan struct{})}
	go func() {
		<-p.Exited()
		close(w.done)
	}()
	return w
}

// Gone is closed once the process has ended.
func (w *Watched) Gone() <-chan struct{} { return w.done }

// Alive reports whether the process has not ended yet. It does not block.
func (w *Watched) Alive() bool {
	select {
	case <-w.done:
		return false
	default:
		return true
	}
}

// Stop asks the process to terminate, waits until it has ended or the grace period of ev is over,
// whichever is first, and then kills its whole group, as a limit or a cancel ends a command. It
// returns once the group is ended. A process that already ended is only killed, which ends what it
// left behind.
func (w *Watched) Stop(ev Events) {
	if w.Alive() && ev.Grace != nil {
		w.p.Terminate()
		fired, stop := ev.Grace()
		select {
		case <-w.done:
		case <-fired:
		}
		stop()
	}
	w.p.Kill()
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
			ev.terminate(svc)
			return end(core.ExecServiceNotReady)
		case <-ev.Startup:
			p.Kill()
			ev.terminate(svc)
			return end(core.ExecServiceNotReady)
		case <-ev.Cancelled:
			p.Kill()
			ev.terminate(svc)
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
			ev.terminate(svc)
			return end(core.ExecServiceNotReady)
		case <-ev.Startup:
			stop()
			ev.terminate(svc)
			return end(core.ExecServiceNotReady)
		case <-ev.Cancelled:
			stop()
			ev.terminate(svc)
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
