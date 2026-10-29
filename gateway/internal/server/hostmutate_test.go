package server

import (
	"context"
	"testing"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
	"google.golang.org/protobuf/proto"
)

func startRunRequest() *apiv1.MutateRequest {
	return &apiv1.MutateRequest{
		Checkout: "/src/checkout",
		Method: &apiv1.MutateRequest_StartRun{StartRun: &hostv1.StartRunRequest{
			RequestId: "request-a", Project: "/project", Task: "mpmc",
		}},
	}
}

func TestMutateAdmissionSurvivesCallerCancel(t *testing.T) {
	parent, cancel := context.WithCancel(context.Background())
	cancel()
	runner := hostcommand.FakeRunner{Program: func(ctx context.Context, _ hostcommand.Command) (hostcommand.Result, error) {
		if err := ctx.Err(); err != nil {
			t.Errorf("runner context ended with the caller: %v", err)
		}
		output, err := proto.Marshal(&apiv1.MutateResponse{Result: &apiv1.MutateResponse_StartRun{StartRun: &hostv1.StartRunResponse{
			RequestId: "request-a", RunId: "run-1", InstanceId: "inst-1",
		}}})
		if err != nil {
			t.Error(err)
		}
		return hostcommand.Result{Output: output}, nil
	}}
	service := &host{config: Config{Root: t.TempDir(), HostRunner: runner}}
	response, err := service.Mutate(parent, startRunRequest())
	if err != nil || response.GetStartRun().GetRunId() != "run-1" || response.GetStartRun().GetInstanceId() != "inst-1" {
		t.Fatalf("Mutate: %v %v", response, err)
	}
}
