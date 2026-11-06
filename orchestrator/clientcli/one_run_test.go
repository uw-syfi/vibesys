package clientcli_test

import (
	"context"
	"fmt"
	"strings"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// A refusal because the repository holds a run tells the user that run's id and how to follow or end it.
func TestAStartRefusedForARunInTheRepositoryNamesTheRunAndHowToReachIt(t *testing.T) {
	f := &fakeService{dispatch: func(cmd *pb.Command) (*pb.DispatchResponse, error) {
		return &pb.DispatchResponse{Receipt: &pb.Receipt{CommandId: cmd.GetId(), Refusal: pb.Refusal_REFUSAL_REPOSITORY_IN_USE, RunId: "run-7"}}, nil
	}}
	w := newWorld(t, serveFake(t, f))
	got := w.run(context.Background(), "run", "start", "--task", "toy")
	if got.code != 1 {
		t.Fatalf("exit %d, want 1", got.code)
	}
	contains(t, "stderr", got.stderr, "run-7", "already working in this repository", "vibesys-client run attach run-7", "vibesys-client run stop run-7")
	if strings.Contains(got.stderr, "REPOSITORY_IN_USE") || strings.Contains(strings.ToLower(got.stderr), "repository in use") {
		t.Errorf("stderr shows the internal name of the refusal: %q", got.stderr)
	}
}

// The same holds with the real daemon in memory: a second start is refused, and the first run lives on.
func TestASecondStartThroughTheClientIsRefusedAndTheFirstRunGoesOn(t *testing.T) {
	l := newLive(t, nil)
	w := newWorld(t, l.Listener.Dial)
	first := w.run(context.Background(), "run", "start", "--task", "toy", "--detach")
	if first.code != 0 {
		t.Fatalf("first start: exit %d, stderr %q", first.code, first.stderr)
	}
	runs := l.Daemon.State().Runs()
	if len(runs) != 1 {
		t.Fatalf("%d runs after the first start", len(runs))
	}
	second := w.run(context.Background(), "run", "start", "--task", "other", "--detach")
	if second.code != 1 {
		t.Fatalf("second start: exit %d, stderr %q", second.code, second.stderr)
	}
	contains(t, "stderr", second.stderr, fmt.Sprint(runs[0].ID))
	if got := len(l.Daemon.State().Runs()); got != 1 {
		t.Fatalf("%d runs after the refused start", got)
	}
}
