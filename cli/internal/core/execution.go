package core

import "time"

// Checkout is a clean checkout of one commit, ready to run in. Whatever makes it owns its
// lifetime.
type Checkout struct {
	Commit Commit
	// Dir is the checkout's directory as the environment sees the host: an absolute path.
	Dir string
}

// Execution is one command to run in a checkout.
type Execution struct {
	Checkout Checkout
	// Scratch, when set, is a directory outside the checkout that the command writes files to and
	// the caller reads after it ends: an absolute path. An environment whose commands do not
	// share the host's files makes it visible at the same path, writable by the command. Empty
	// means the command has none.
	Scratch string
	// Artifacts, when set, is an empty directory outside the checkout that the command may leave
	// files in for a person to read after the job: an absolute path. An environment whose
	// commands do not share the host's files brings what they leave there back to it. Empty means
	// the command has none.
	Artifacts string
	Argv      []string
	// Service, when set, is started first and the command runs against it.
	Service *ServiceSpec
	// Timeout bounds the whole execution. It must be positive.
	Timeout time.Duration
}

// ExecKind is how an execution that the environment saw end ended. The set is closed.
type ExecKind string

// The execution endings. An execution whose end the environment did not see has none: it is an
// Unobserved attempt, not an ending.
const (
	ExecExited          ExecKind = "exited"
	ExecTimedOut        ExecKind = "timed_out"
	ExecCancelled       ExecKind = "cancelled"
	ExecServiceNotReady ExecKind = "service_not_ready"
	ExecServiceExited   ExecKind = "service_exited"
)

// ExecOutcome is how an execution ended.
type ExecOutcome struct {
	Kind ExecKind
	// ExitCode is set when Kind is ExecExited.
	ExitCode int
}

// Attempt is what one attempt to run an execution did, as the environment knows it. The set is
// closed, so a caller tells a command that never started from one that ran and whose end is not
// known: only the first can be run again without repeating anything.
type Attempt interface{ attempt() }

// NotStarted says no process of the execution started, so nothing of it ran. Failure says why.
type NotStarted struct{ Failure Failure }

// Ended says the execution started and the environment saw it end as Exec says.
type Ended struct{ Exec ExecOutcome }

// Unobserved says a process of the execution started, and the environment cannot vouch for how it
// ended or for what it left: its end was not seen, its output was not all kept, or its container
// was not removed. Failure says what was lost.
type Unobserved struct{ Failure Failure }

func (NotStarted) attempt() {}
func (Ended) attempt()      {}
func (Unobserved) attempt() {}
