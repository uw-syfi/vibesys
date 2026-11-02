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
	Argv    []string
	// Service, when set, is started first and the command runs against it.
	Service *ServiceSpec
	// Timeout bounds the whole execution. It must be positive.
	Timeout time.Duration
}

// ExecKind is how an execution ended.
type ExecKind string

// The execution endings. ExecUnknown means the environment cannot say what happened, such as a
// lost connection to a container, so a caller does not read it as success or failure.
const (
	ExecExited          ExecKind = "exited"
	ExecTimedOut        ExecKind = "timed_out"
	ExecCancelled       ExecKind = "cancelled"
	ExecServiceNotReady ExecKind = "service_not_ready"
	ExecServiceExited   ExecKind = "service_exited"
	ExecUnknown         ExecKind = "unknown"
)

// ExecOutcome is what an execution did.
type ExecOutcome struct {
	Kind ExecKind
	// ExitCode is set when Kind is ExecExited.
	ExitCode int
}
