package daemon

import (
	"context"
	"fmt"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// The ports of the New run contracts. Each is the one way the daemon learns a fact it does not hold:
// what a task says, where evaluations can run, which examples exist, and how a repository is made.
// A production implementation of each is a later item's; until then the rpcs that need them answer
// Unimplemented (see the handlers below), and a test uses the Fakes in daemontest.

// TaskSource says where a task is read from.
type TaskSource int

// The places a task is read from.
const (
	// TaskCommitted is the task as committed in a repository, at Revision (HEAD when empty).
	TaskCommitted TaskSource = iota + 1
	// TaskWorkingTree is the task as it stands in the repository's working tree: a draft.
	TaskWorkingTree
	// TaskFolder is a task in a folder under Repository that is not a repository: the release's examples.
	TaskFolder
)

// TaskRef names a task to read.
type TaskRef struct {
	Source TaskSource
	// Repository is the repository's absolute path, or the folder's for TaskFolder.
	Repository string
	Revision   string
	Task       string
}

// TaskUnreadableReason says why a task has no card.
type TaskUnreadableReason string

// The reasons.
const (
	TaskNotFound    TaskUnreadableReason = "not_found"
	TaskInvalid     TaskUnreadableReason = "invalid"
	TaskUnsupported TaskUnreadableReason = "unsupported"
)

// TaskUnreadable is the error of a task that has no card; Detail names the key or the path.
type TaskUnreadable struct {
	Reason TaskUnreadableReason
	Detail string
}

func (e *TaskUnreadable) Error() string {
	return fmt.Sprintf("task unreadable (%s): %s", e.Reason, e.Detail)
}

// TaskReader reads the card of a task. Only the CLI's daemon reads task files; this is the call to it.
// An unreadable task is a *TaskUnreadable; any other error is a failure of the call, to retry.
type TaskReader interface {
	Read(ctx context.Context, ref TaskRef) (core.TaskCard, error)
}

func unimplemented(rpc string) error {
	return status.Errorf(codes.Unimplemented, "%s is not implemented by this host yet", rpc)
}
