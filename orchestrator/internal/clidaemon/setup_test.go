package clidaemon_test

import (
	"context"
	"errors"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
)

// fakeSetup is a daemon's answer to DescribeSetup, which counts the requests it was asked.
type fakeSetup struct {
	resp     *cli.DescribeSetupResponse
	err      error
	requests []*cli.DescribeSetupRequest
}

func (f *fakeSetup) DescribeSetup(_ context.Context, r *cli.DescribeSetupRequest) (*cli.DescribeSetupResponse, error) {
	f.requests = append(f.requests, r)
	return f.resp, f.err
}

func validFacts() *cli.SetupFacts {
	base := strings.Repeat("a", 40)
	return &cli.SetupFacts{
		Task: "kv", TaskVersion: "v1",
		Card:         &cli.TaskCard{Task: "kv"},
		Declarations: &cli.SetupDeclarations{},
		Base:         &cli.SetupBase{Commit: base, TaskVersion: "v1"},
		Jobs: []*cli.SetupJob{{
			JobId: "j", Kind: cli.JobKind_JOB_KIND_CHECK, State: cli.JobState_JOB_STATE_SUCCEEDED, Commit: base, TaskVersion: "v1",
			Relation: cli.SetupCommitRelation_SETUP_COMMIT_RELATION_BASE, SubmittedAt: timestamppb.Now(),
		}},
	}
}

func TestDescribeSetupAsksForTheRepositoryAndTaskAndReturnsWhatTheDaemonSaid(t *testing.T) {
	facts := &fakeSetup{resp: &cli.DescribeSetupResponse{Outcome: &cli.DescribeSetupResponse_Facts{Facts: validFacts()}}}
	got, err := clidaemon.DescribeSetup(context.Background(), facts, "/repo", "kv")
	if err != nil || got.Facts == nil || got.Unreadable != "" || got.Facts.GetTask() != "kv" {
		t.Fatalf("%+v, %v", got, err)
	}
	if r := facts.requests[0]; r.GetRepository() != "/repo" || r.GetTask() != "kv" {
		t.Fatalf("asked %v", r)
	}

	unreadable := &fakeSetup{resp: &cli.DescribeSetupResponse{Outcome: &cli.DescribeSetupResponse_Unreadable{Unreadable: &cli.TaskUnreadable{
		Reason: cli.TaskUnreadableReason_TASK_UNREADABLE_REASON_INVALID, Detail: "objectives.toml: unknown key"}}}}
	got, err = clidaemon.DescribeSetup(context.Background(), unreadable, "/repo", "kv")
	if err != nil || got.Facts != nil || got.Unreadable != "objectives.toml: unknown key" {
		t.Fatalf("%+v, %v", got, err)
	}
}

// Whatever else goes wrong, the answer is an error and never facts: a daemon that does not know the
// call, one that fails, and one whose facts break the contract's own rules.
func TestDescribeSetupThatWasNotAnsweredWellIsAnErrorNeverFacts(t *testing.T) {
	broken := validFacts()
	broken.Jobs[0].Commit = strings.Repeat("b", 40) // a base job that is not at the base commit
	for name, tc := range map[string]struct {
		daemon *fakeSetup
		want   string
	}{
		"unimplemented": {&fakeSetup{err: status.Error(codes.Unimplemented, "method DescribeSetup not implemented")}, "cannot describe"},
		"failed":        {&fakeSetup{err: errors.New("connection reset")}, "connection reset"},
		"invalid":       {&fakeSetup{resp: &cli.DescribeSetupResponse{Outcome: &cli.DescribeSetupResponse_Facts{Facts: broken}}}, "not valid"},
		"empty":         {&fakeSetup{resp: &cli.DescribeSetupResponse{}}, "not valid"},
	} {
		got, err := clidaemon.DescribeSetup(context.Background(), tc.daemon, "/repo", "kv")
		if err == nil || !strings.Contains(err.Error(), tc.want) || got.Facts != nil || got.Unreadable != "" {
			t.Errorf("%s: %+v, %v", name, got, err)
		}
	}
}
