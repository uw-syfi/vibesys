package wire_test

import (
	"testing"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// A run recorded before phases existed has no phase field. It must still meet the contract, and every
// reader must take it as optimizing; a run in setup may have no task yet, and no other may lack one.
func TestARunWithoutAPhaseIsOptimizingAndASetupRunNeedsNoTask(t *testing.T) {
	at := timestamppb.New(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	old := &pb.Run{Id: "r1", Repository: "/repo", Task: "task", Status: pb.RunStatus_RUN_STATUS_RUNNING, LeadAgentId: "a1", CreatedAt: at}
	if err := wire.Validate(old); err != nil {
		t.Fatalf("an old run (no phase) breaks the contract: %v", err)
	}
	data, err := proto.Marshal(&pb.Event{Sequence: 1, RunId: "r1", At: at, Body: &pb.Event_RunUpdated{RunUpdated: &pb.RunRecord{Run: old}}})
	if err != nil {
		t.Fatal(err)
	}
	_, event, err := wire.DecodeEvent(data)
	if err != nil {
		t.Fatal(err)
	}
	run, ok := event.Body.(core.Run)
	if !ok || !run.Phase.Optimizing() {
		t.Fatalf("an old run reads as %+v, want optimizing", event.Body)
	}

	taskless := proto.Clone(old).(*pb.Run)
	taskless.Task = ""
	if err := wire.Validate(taskless); err == nil {
		t.Error("a run that is not in setup and has no task met the contract")
	}
	taskless.Phase = pb.RunPhase_RUN_PHASE_SETUP
	if err := wire.Validate(taskless); err != nil {
		t.Errorf("a run in setup with no task yet breaks the contract: %v", err)
	}
}
