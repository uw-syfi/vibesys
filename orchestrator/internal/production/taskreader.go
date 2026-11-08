package production

import (
	"context"
	"errors"
	"fmt"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// TaskDescriber is the one call of the CLI's daemon this reader makes. Only that daemon reads task
// files; clidaemon.Client is the real one, over the socket the job tool answered.
type TaskDescriber interface {
	DescribeTask(context.Context, *cliv1.DescribeTaskRequest) (*cliv1.DescribeTaskResponse, error)
}

// TaskReader is a daemon.TaskReader over the CLI's DescribeTask. It parses no task file: the callee
// owns the schema, so a new field of a card is the CLI's edit alone. What crosses the process is held to
// its contract in both directions, and a card for another task than the one asked is refused.
type TaskReader struct{ Describer TaskDescriber }

var _ daemon.TaskReader = TaskReader{}

// Read implements daemon.TaskReader. A task the daemon cannot read is a *daemon.TaskUnreadable with
// the daemon's detail; a failed call, or an answer that breaks the contract, is any other error.
func (r TaskReader) Read(ctx context.Context, ref daemon.TaskRef) (core.TaskCard, error) {
	req, err := describeRequest(ref)
	if err != nil {
		return core.TaskCard{}, err
	}
	if err := wire.Validate(req); err != nil {
		return core.TaskCard{}, fmt.Errorf("describe task %s: %w", ref.Task, err)
	}
	resp, err := r.Describer.DescribeTask(ctx, req)
	if err != nil {
		return core.TaskCard{}, fmt.Errorf("describe task %s: %w", ref.Task, err)
	}
	if err := wire.Validate(resp); err != nil {
		return core.TaskCard{}, fmt.Errorf("describe task %s: the daemon's answer: %w", ref.Task, err)
	}
	if u := resp.GetUnreadable(); u != nil {
		return core.TaskCard{}, &daemon.TaskUnreadable{Reason: unreadableReasons[u.GetReason()], Detail: u.GetDetail()}
	}
	card, err := wire.TaskCardFromMessage(resp.GetCard())
	if err != nil {
		return core.TaskCard{}, fmt.Errorf("describe task %s: the daemon's card: %w", ref.Task, err)
	}
	if card.Task != ref.Task {
		return core.TaskCard{}, fmt.Errorf("describe task %s: the daemon answered the card of task %s", ref.Task, card.Task)
	}
	return card, nil
}

var unreadableReasons = map[cliv1.TaskUnreadableReason]daemon.TaskUnreadableReason{
	cliv1.TaskUnreadableReason_TASK_UNREADABLE_REASON_NOT_FOUND:   daemon.TaskNotFound,
	cliv1.TaskUnreadableReason_TASK_UNREADABLE_REASON_INVALID:     daemon.TaskInvalid,
	cliv1.TaskUnreadableReason_TASK_UNREADABLE_REASON_UNSUPPORTED: daemon.TaskUnsupported,
}

func describeRequest(ref daemon.TaskRef) (*cliv1.DescribeTaskRequest, error) {
	switch ref.Source {
	case daemon.TaskCommitted:
		committed := &cliv1.CommittedTask{Repository: ref.Repository, Task: ref.Task}
		if ref.Revision != "" {
			committed.Revision = &ref.Revision
		}
		return &cliv1.DescribeTaskRequest{Source: &cliv1.DescribeTaskRequest_Committed{Committed: committed}}, nil
	case daemon.TaskWorkingTree:
		return &cliv1.DescribeTaskRequest{Source: &cliv1.DescribeTaskRequest_WorkingTree{WorkingTree: &cliv1.WorkingTreeTask{Repository: ref.Repository, Task: ref.Task}}}, nil
	case daemon.TaskFolder:
		return &cliv1.DescribeTaskRequest{Source: &cliv1.DescribeTaskRequest_Folder{Folder: &cliv1.FolderTask{Root: ref.Repository, Task: ref.Task}}}, nil
	}
	return nil, errors.New("describe task: the reference names no source")
}
