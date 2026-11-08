package task

import (
	"context"

	"buf.build/go/protovalidate"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Describe answers a DescribeTask request: it reads the task's files, from where the request says,
// and answers its card, or why the files have none: a task or place that is not there (NOT_FOUND), or
// a file that is invalid, with the reader's words naming the file and the key (INVALID). Anything else
// that went wrong is an error, so a client never takes a daemon's trouble for a task with no card. A
// request that breaks its rules is an invalid-input failure, and files that are nil are refused as
// unavailable.
func Describe(ctx context.Context, files CardFiles, req *cliv1.DescribeTaskRequest) (*cliv1.DescribeTaskResponse, error) {
	if err := protovalidate.Validate(req); err != nil {
		return nil, invalid("describe task: %v", err)
	}
	if files == nil {
		return nil, unsupported("this daemon has no reader of task files")
	}
	name, tree, err := cardFilesOf(ctx, files, req)
	if err == nil {
		var card *cliv1.TaskCard
		if card, err = Card(tree, name); err == nil {
			return &cliv1.DescribeTaskResponse{Outcome: &cliv1.DescribeTaskResponse_Card{Card: card}}, nil
		}
	}
	f := core.AsFailure(err)
	var reason cliv1.TaskUnreadableReason
	switch f.Kind {
	case core.FailureNotFound:
		reason = cliv1.TaskUnreadableReason_TASK_UNREADABLE_REASON_NOT_FOUND
	case core.FailureInvalidInput:
		reason = cliv1.TaskUnreadableReason_TASK_UNREADABLE_REASON_INVALID
	default:
		return nil, err
	}
	return &cliv1.DescribeTaskResponse{Outcome: &cliv1.DescribeTaskResponse_Unreadable{
		Unreadable: &cliv1.TaskUnreadable{Reason: reason, Detail: f.Message},
	}}, nil
}

func cardFilesOf(ctx context.Context, files CardFiles, req *cliv1.DescribeTaskRequest) (string, Tree, error) {
	switch s := req.GetSource().(type) {
	case *cliv1.DescribeTaskRequest_Committed:
		tree, err := files.Committed(ctx, s.Committed.GetRepository(), s.Committed.GetRevision(), s.Committed.GetTask())
		return s.Committed.GetTask(), tree, err
	case *cliv1.DescribeTaskRequest_Folder:
		tree, err := files.Folder(ctx, s.Folder.GetRoot(), s.Folder.GetTask())
		return s.Folder.GetTask(), tree, err
	case *cliv1.DescribeTaskRequest_WorkingTree:
		tree, err := files.WorkingTree(ctx, s.WorkingTree.GetRepository(), s.WorkingTree.GetTask())
		return s.WorkingTree.GetTask(), tree, err
	}
	return "", nil, invalid("describe task: no source")
}
