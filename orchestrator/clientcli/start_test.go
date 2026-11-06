package clientcli_test

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

func TestStartFollowsTheStreamUntilTheLeadIsLive(t *testing.T) {
	l := newLive(t, nil)
	w := newWorld(t, l.Listener.Dial)
	got := w.run(context.Background(), "run", "start", "--task", "toy", "--brief", "go")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "started in /work/repo (task toy)", "is live")
	contains(t, "stderr", got.stderr, "agents run as your user with their permissions bypassed and no confinement")
	state := l.Daemon.State()
	if len(state.Runs()) != 1 {
		t.Fatalf("want one run, found %d", len(state.Runs()))
	}
	for _, r := range state.Runs() {
		if r.Repository != "/work/repo" || r.Task != "toy" {
			t.Errorf("run = %+v", r)
		}
	}
}

func TestStartJSONPrintsOnlyTheReceiptOnStdout(t *testing.T) {
	l := newLive(t, nil)
	w := newWorld(t, l.Listener.Dial)
	got := w.run(context.Background(), "run", "start", "--task", "toy", "--json")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	var receipt pb.Receipt
	if err := protojson.Unmarshal([]byte(got.stdout), &receipt); err != nil {
		t.Fatalf("stdout %q is not a receipt: %v", got.stdout, err)
	}
	if receipt.GetRunId() == "" || receipt.GetCommandId() != "cmd-1" {
		t.Errorf("receipt = %v", &receipt)
	}
	if strings.Count(got.stdout, "\n") != 1 || !json.Valid([]byte(got.stdout)) {
		t.Errorf("stdout is not one line of JSON: %q", got.stdout)
	}
	contains(t, "stderr", got.stderr, "no confinement")
}

func TestStartOnARunThatFailsExitsOneWithTheAgentsReason(t *testing.T) {
	l := newLive(t, nil)
	l.Sidecars.LaunchFailures = 1000
	l.Sidecars.LaunchError = errors.New("the agent image is missing")
	w := newWorld(t, l.Listener.Dial)
	got := w.run(context.Background(), "run", "start", "--task", "toy")
	if got.code != 1 {
		t.Fatalf("exit %d, want 1; stdout %q stderr %q", got.code, got.stdout, got.stderr)
	}
	contains(t, "stderr", got.stderr, "failed", "the agent image is missing")
}

func TestStartRefusalPrintsTheServicesReason(t *testing.T) {
	f := &fakeService{dispatch: func(cmd *pb.Command) (*pb.DispatchResponse, error) {
		return &pb.DispatchResponse{Receipt: &pb.Receipt{CommandId: cmd.GetId(), Refusal: pb.Refusal_REFUSAL_DEADLINE_PASSED}}, nil
	}}
	w := newWorld(t, serveFake(t, f))
	got := w.run(context.Background(), "run", "start", "--task", "toy", "--deadline", "2020-01-01T00:00:00Z")
	if got.code != 1 {
		t.Fatalf("exit %d", got.code)
	}
	contains(t, "stderr", got.stderr, "refused: deadline passed")
}

func TestStartSendsEveryFlagInTheCommand(t *testing.T) {
	f := &fakeService{dispatch: func(cmd *pb.Command) (*pb.DispatchResponse, error) {
		return &pb.DispatchResponse{Receipt: &pb.Receipt{CommandId: cmd.GetId(), Refusal: pb.Refusal_REFUSAL_RUN_NOT_FOUND}}, nil
	}}
	w := newWorld(t, serveFake(t, f))
	w.dir = "/elsewhere"
	w.run(context.Background(), "run", "start", "--task", "toy", "--repository", "rel/repo", "--brief", "b", "--harness", "codex",
		"--fanout", "7", "--model", "m", "--effort", "high", "--deadline", "2030-01-02T03:04:05Z")
	sent := f.sent()
	if len(sent) != 1 {
		t.Fatalf("sent %d commands", len(sent))
	}
	got := sent[0].GetStartRun()
	if got.GetRepository() != "/elsewhere/rel/repo" || got.GetTask() != "toy" || got.GetBrief() != "b" || got.GetFanoutLimit() != 7 ||
		got.GetLead().GetHarness() != pb.Harness_HARNESS_CODEX || got.GetLead().GetModel() != "m" || got.GetLead().GetReasoningEffort() != "high" ||
		got.GetDeadline().AsTime().Year() != 2030 {
		t.Errorf("StartRun = %v", got)
	}
}

// A person at a terminal who starts a run is attached once the lead is live; Ctrl-C then detaches
// and the run goes on.
func TestStartOnATerminalAttachesAndCtrlCDetachesWithoutStopping(t *testing.T) {
	l := newLive(t, nil)
	w := newWorld(t, l.Listener.Dial)
	w.terminal = true
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w.onStdout = func(written string) {
		if strings.Contains(written, "turn 1 completed") {
			cancel() // Ctrl-C, after the attach has shown the lead's first turn.
		}
	}
	got := w.runUnbounded(ctx, "run", "start", "--task", "toy")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stderr", got.stderr, "Ctrl-C detaches", "is still going", "vibesys-client run attach", "vibesys-client run stop")
	for _, r := range l.Daemon.State().Runs() {
		if r.Status != core.RunRunning {
			t.Errorf("Ctrl-C left the run %s", r.Status)
		}
	}
	for _, s := range l.Sidecars.Sessions() {
		for _, cmd := range s.Commands() {
			if cmd.GetClose() != nil || cmd.GetInterrupt() != nil {
				t.Errorf("Ctrl-C sent %v to a session", cmd)
			}
		}
	}
}

func TestStartWithDetachOrNoTerminalDoesNotAttach(t *testing.T) {
	for _, terminal := range []bool{false, true} {
		l := newLive(t, nil)
		w := newWorld(t, l.Listener.Dial)
		w.terminal = terminal
		args := []string{"run", "start", "--task", "toy"}
		if terminal {
			args = append(args, "--detach")
		}
		got := w.run(context.Background(), args...)
		if got.code != 0 || strings.Contains(got.stderr, "Ctrl-C") {
			t.Errorf("terminal %v: exit %d, stderr %q", terminal, got.code, got.stderr)
		}
	}
}
