package environment

import (
	"context"
	"fmt"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Bench is a lab's containers while they are up, as an environment hands them to the lab that
// holds them (Environment.Lab). Everything in it is valid until hold returns.
type Bench struct {
	// Rooms are where the lab's commands start, by room: the candidate's always, and the task's for
	// a task of two images. A process started in a room runs as the invoking user in the checkout,
	// and ends when the lab does.
	Rooms map[core.LabRoom]Launcher
	// Port is a port of the candidate's room on which nothing listened when it started; the task's
	// room reaches it at 127.0.0.1.
	Port int
	// Artifacts is where every room sees the artifacts directory, which they share.
	Artifacts string
	// Shell says the rooms run /bin/sh, as an image does, and share a process namespace with what
	// runs in them: the lab then signals one process (and, for a kill, what it started) through
	// it. A container's own Terminate and Kill reach every process of it, or only the client of
	// one. Without it the lab uses a process's Terminate and Kill, which in a room whose processes
	// are each a group of their own, as a fake's are, reach that process's alone.
	Shell bool
	// Limit is closed when the lab's time is up, and Cancelled when the caller cancels it.
	Limit, Cancelled <-chan struct{}
	// Grace returns a channel closed once the processes of the lab have had their time to end after
	// they were asked to, and the function that releases it.
	Grace func() (fired <-chan struct{}, stop func())
	// Collect brings what the rooms wrote to the artifacts directory back to the caller's
	// (core.Execution.Artifacts), so the caller reads it now and not only after the lab.
	Collect func(ctx context.Context) error
	// Replaced makes the rooms see the caller's checkout as it is now, after it was replaced in
	// place: its files and no others.
	Replaced func(ctx context.Context) error
	// Added makes the rooms see the file at rel of the caller's artifacts directory, which the
	// caller wrote there. Nothing the rooms wrote is removed.
	Added func(ctx context.Context, rel core.LabPath) error
}

// HoldLab is what holds a lab once its containers are up: it runs what the lab's calls ask in the
// bench's rooms, and returns how the lab ended, once it ended: ExecExited with code 0 when its time
// was up or it was asked to end, ExecCancelled when the bench's Cancelled closed. It ends every
// process it started, and asks them to terminate first, before it returns.
type HoldLab func(Bench) core.ExecOutcome

// NoLab is what an environment that holds no labs answers Lab with: nothing started.
func NoLab(kind core.EnvironmentKind) core.Attempt {
	return core.NotStarted{Failure: core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
		"the %s environment holds no lab: a lab holds the containers of a placement, so run it in docker", kind)}}
}

// ValidateLab refuses a lab's execution no environment can hold, as Validate does a command's: one
// with a command, a service or a candidate's server of its own (a lab runs what its calls ask), or
// with no time, checkout or artifacts directory.
func ValidateLab(x core.Execution) error {
	invalid := func(format string, args ...any) error {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: "lab: " + fmt.Sprintf(format, args...)}
	}
	switch {
	case len(x.Argv) > 0 || x.Service != nil:
		return invalid("a lab runs what its calls ask, and no command or service of its own")
	case x.Candidate != nil && x.Candidate.Serve != nil:
		return invalid("a lab starts no candidate's server of its own; its caller starts one")
	case x.Artifacts == "":
		return invalid("a lab has an artifacts directory, which its rooms share")
	case x.Measured || x.Later > 0:
		return invalid("a lab is never measured, and no execution follows it")
	}
	x.Argv = []string{"lab"}
	return Validate(x)
}
