package production

import (
	"context"
	"errors"
	"fmt"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
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

// TaskConnection is a TaskDescriber the caller closes.
type TaskConnection interface {
	TaskDescriber
	Close() error
}

// CLITasks is the host's task reader over the job tool's daemon: each read asks the tool where its
// daemon is, so a repaired or replaced install is used by the next read, then asks the daemon.
type CLITasks struct {
	// JobTool is the endpoint of the vibesys-cli the host's runs use.
	JobTool func(context.Context) (core.JobToolEndpoint, error)
	// Runner runs the tool.
	Runner Runner
	// Connect dials the socket the tool answered.
	Connect func(context.Context, clidaemon.Socket) (TaskConnection, error)
}

var _ daemon.TaskReader = CLITasks{}

// NewCLITasks is the reader of a real host.
func NewCLITasks(jobTool func(context.Context) (core.JobToolEndpoint, error)) CLITasks {
	return CLITasks{JobTool: jobTool, Runner: ExecRunner{}, Connect: func(_ context.Context, s clidaemon.Socket) (TaskConnection, error) {
		return clidaemon.NewClient(s, CLIUnixDialer{})
	}}
}

// Read implements daemon.TaskReader.
func (t CLITasks) Read(ctx context.Context, ref daemon.TaskRef) (core.TaskCard, error) {
	endpoint, err := t.JobTool(ctx)
	if err != nil {
		return core.TaskCard{}, err
	}
	socket, err := ExecEnsurerFor(endpoint, t.Runner).Ensure(ctx)
	if err != nil {
		return core.TaskCard{}, err
	}
	conn, err := t.Connect(ctx, socket)
	if err != nil {
		return core.TaskCard{}, err
	}
	defer conn.Close()
	return TaskReader{conn}.Read(ctx, ref)
}
