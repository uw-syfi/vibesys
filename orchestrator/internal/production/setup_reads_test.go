package production

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
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/setup"
)

// setupDaemon answers DescribeSetup with what it is given, after the gate (if any) opens.
type setupDaemon struct {
	resp    *cli.DescribeSetupResponse
	err     error
	gate    chan struct{}
	asked   *cli.DescribeSetupRequest
	closed  int
	started chan struct{}
}

func (d *setupDaemon) DescribeSetup(_ context.Context, r *cli.DescribeSetupRequest) (*cli.DescribeSetupResponse, error) {
	d.asked = r
	if d.started != nil {
		close(d.started)
	}
	if d.gate != nil {
		<-d.gate
	}
	return d.resp, d.err
}

func (d *setupDaemon) Close() error { d.closed++; return nil }

func factsResponse() *cli.DescribeSetupResponse {
	base := strings.Repeat("c", 40)
	return &cli.DescribeSetupResponse{Outcome: &cli.DescribeSetupResponse_Facts{Facts: &cli.SetupFacts{
		Task: "kv", TaskVersion: "v1", Card: &cli.TaskCard{Task: "kv", Heading: "Fast KV"},
		Declarations: &cli.SetupDeclarations{CheckDeclared: true},
		Base:         &cli.SetupBase{Commit: base, TaskVersion: "v1"},
		Jobs: []*cli.SetupJob{{
			JobId: "j1", Kind: cli.JobKind_JOB_KIND_CHECK, State: cli.JobState_JOB_STATE_FAILED, Commit: base, TaskVersion: "v1",
			Relation: cli.SetupCommitRelation_SETUP_COMMIT_RELATION_BASE, SubmittedAt: timestamppb.Now(), FailedStage: cli.JobStage_JOB_STAGE_CHECK,
		}},
	}}}
}

func setupReadsOver(tool *answeringTool, d *setupDaemon) CLISetupReads {
	return CLISetupReads{Runner: tool, Connect: func(context.Context, clidaemon.Socket) (clidaemon.SetupConnection, error) { return d, nil }}
}

var someRef = daemon.SetupRef{JobTool: core.JobToolEndpoint{Executable: "/opt/vibesys/bin/vibesys-cli"}, Repository: "/repo", Task: "kv"}

// readOnce starts a read and waits for its answer.
func readOnce(t *testing.T, c CLISetupReads) setup.Read {
	t.Helper()
	got := make(chan setup.Read, 1)
	c.Start(bounded(t), someRef, func(r setup.Read) { got <- r })
	return <-got
}

func TestACLISetupReadAsksTheToolThenTheDaemonAndMapsTheFacts(t *testing.T) {
	tool, d := &answeringTool{}, &setupDaemon{resp: factsResponse()}
	r := readOnce(t, setupReadsOver(tool, d))
	if r.Kind != setup.ReadFacts || r.Facts.Task != "kv" || r.Facts.Card.Heading != "Fast KV" {
		t.Fatalf("%+v", r)
	}
	if got := r.Facts.Jobs[0]; got.FailedStage != setup.StageCheck || got.State != setup.JobFailed {
		t.Fatalf("the failed stage did not arrive: %+v", got)
	}
	if d.asked.GetRepository() != "/repo" || d.asked.GetTask() != "kv" || d.closed != 1 {
		t.Fatalf("asked %v, closed %d", d.asked, d.closed)
	}
	if len(tool.ran) != 1 || tool.ran[0] != "/opt/vibesys/bin/vibesys-cli" {
		t.Fatalf("ran %v, want the run's tool once", tool.ran)
	}
}

// A task that cannot be read is said so in the reader's words; every way the read goes wrong is a
// failed read, which no one takes for facts.
func TestACLISetupReadSaysWhatWentWrongAndNeverPasses(t *testing.T) {
	unreadable := &cli.DescribeSetupResponse{Outcome: &cli.DescribeSetupResponse_Unreadable{Unreadable: &cli.TaskUnreadable{
		Reason: cli.TaskUnreadableReason_TASK_UNREADABLE_REASON_INVALID, Detail: "manifest.toml: unknown key"}}}
	if r := readOnce(t, setupReadsOver(&answeringTool{}, &setupDaemon{resp: unreadable})); r.Kind != setup.ReadUnreadable || r.Detail != "manifest.toml: unknown key" {
		t.Fatalf("%+v", r)
	}
	for name, d := range map[string]*setupDaemon{
		"older daemon": {err: status.Error(codes.Unimplemented, "nope")},
		"failed":       {err: errors.New("reset")},
		"no outcome":   {resp: &cli.DescribeSetupResponse{}},
	} {
		if r := readOnce(t, setupReadsOver(&answeringTool{}, d)); r.Kind != setup.ReadFailure || r.Detail == "" {
			t.Errorf("%s: %+v", name, r)
		}
	}
	gone := CLISetupReads{Runner: absentTool{}, Connect: func(context.Context, clidaemon.Socket) (clidaemon.SetupConnection, error) {
		return nil, errors.New("not dialed")
	}}
	if r := readOnce(t, gone); r.Kind != setup.ReadFailure {
		t.Fatalf("a tool that is not there: %+v", r)
	}
}

// Start returns at once, before the daemon has answered: the loop that calls it goes on while the read
// is out, and the answer comes through the callback when it arrives.
func TestACLISetupReadStartReturnsBeforeTheDaemonAnswers(t *testing.T) {
	d := &setupDaemon{resp: factsResponse(), gate: make(chan struct{}), started: make(chan struct{})}
	got := make(chan setup.Read, 1)
	setupReadsOver(&answeringTool{}, d).Start(bounded(t), someRef, func(r setup.Read) { got <- r })
	<-d.started // the daemon holds the read, and Start has returned
	select {
	case r := <-got:
		t.Fatalf("answered before the daemon did: %+v", r)
	default:
	}
	close(d.gate)
	if r := <-got; r.Kind != setup.ReadFacts {
		t.Fatalf("%+v", r)
	}
}

// absentTool is a host whose vibesys-cli is not there.
type absentTool struct{}

func (absentTool) Run(context.Context, string, []string, []string) ([]byte, []byte, error) {
	return nil, nil, errors.New("no vibesys-cli")
}
