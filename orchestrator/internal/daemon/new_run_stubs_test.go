package daemon_test

import (
	"context"
	"errors"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// Until a later item implements them, the rpcs of the New run contracts answer Unimplemented, and say
// which one, rather than an empty success a client would take for "nothing there".
func TestTheNewRunRpcsAreUnimplementedAndNameThemselves(t *testing.T) {
	r := daemontest.NewRig(t)
	ctx := context.Background()
	calls := map[string]func() error{
		"ProvisionRepository": func() error {
			_, err := r.Service.ProvisionRepository(ctx, &pb.ProvisionRepositoryRequest{})
			return err
		},
	}
	for name, call := range calls {
		err := call()
		if status.Code(err) != codes.Unimplemented || err == nil || !strings.Contains(err.Error(), name) {
			t.Errorf("%s: got %v, want Unimplemented naming the rpc", name, err)
		}
	}
}

// The commands of the New run contracts are parsed and reach the core, which refuses each of them
// as not permitted until its behavior exists: no run is made or changed.
func TestTheNewRunCommandsAreRefusedUntilDecided(t *testing.T) {
	spec := &pb.AgentSpec{Harness: pb.Harness_HARNESS_CODEX}
	bodies := map[string]*pb.Command{
		"update_run": {Id: "u1", Body: &pb.Command_UpdateRun{UpdateRun: &pb.UpdateRun{
			RunId: "run-1", CostLimit: &pb.CostLimitUpdate{Change: &pb.CostLimitUpdate_Remove{Remove: true}},
		}}},
		"begin_setup": {Id: "b1", Body: &pb.Command_BeginSetup{BeginSetup: &pb.BeginSetup{Repository: "/repo", Lead: spec}}},
		"complete_setup": {Id: "c1", Body: &pb.Command_CompleteSetup{CompleteSetup: &pb.CompleteSetup{
			RunId: "run-1", Task: "task", Lead: spec,
		}}},
	}
	for name, cmd := range bodies {
		t.Run(name, func(t *testing.T) {
			r := daemontest.NewRig(t)
			receipt := dispatch(t, r, cmd)
			if receipt.GetRefusal() != pb.Refusal_REFUSAL_NOT_PERMITTED {
				t.Fatalf("receipt %v, want a refusal as not permitted", receipt)
			}
			if n := len(r.Daemon.State().Runs()); n != 0 {
				t.Fatalf("%d runs, want none", n)
			}
		})
	}
}

// Each Fake satisfies its port and answers as the contract says: an unknown task is unreadable and
// named, places and catalogue are as set, a provision is remembered.
func TestTheNewRunFakesAnswerAsTheirPortsDo(t *testing.T) {
	ctx := context.Background()

	var tasks daemontest.FakeTaskReader
	card := core.TaskCard{Task: "flint", Domain: core.DomainLLMServing}
	tasks.Set("flint", card)
	if got, err := tasks.Read(ctx, daemon.TaskRef{Source: daemon.TaskCommitted, Repository: "/repo", Task: "flint"}); err != nil || got.Task != "flint" {
		t.Fatalf("read %v, %v", got, err)
	}
	var unreadable *daemon.TaskUnreadable
	if _, err := tasks.Read(ctx, daemon.TaskRef{Task: "missing"}); !errors.As(err, &unreadable) || unreadable.Reason != daemon.TaskNotFound {
		t.Fatalf("a task with no card: %v, want unreadable as not found", err)
	}

	var places daemontest.FakePlaceSource
	local := &pb.EvalPlace{Name: "local", Kind: pb.EvalPlaceKind_EVAL_PLACE_KIND_LOCAL}
	places.Set(local)
	got, err := places.Places(ctx, true)
	if err != nil || len(got) != 1 || got[0] != local || places.Refreshes() != 1 {
		t.Fatalf("places %v, %v, %d refreshes", got, err, places.Refreshes())
	}

	var catalogue daemontest.FakeCatalogueSource
	catalogue.Set(daemon.Catalogue{Release: "r1", RepositoriesDirectory: "/repos"})
	if c, err := catalogue.Catalogue(ctx); err != nil || c.Release != "r1" {
		t.Fatalf("catalogue %v, %v", c, err)
	}

	var provisioner daemontest.FakeProvisioner
	req := &pb.ProvisionRepositoryRequest{RequestId: "q1", Name: "mine", Source: &pb.ProvisionRepositoryRequest_Empty{Empty: &pb.EmptyRepository{}}}
	res, err := provisioner.Provision(ctx, req)
	if err != nil || res.GetProvisioned().GetRepository() != "/repositories/mine" {
		t.Fatalf("provision %v, %v", res, err)
	}
	if calls := provisioner.Calls(); len(calls) != 1 || !proto.Equal(calls[0], req) {
		t.Fatalf("calls %v, want the request", calls)
	}
}
