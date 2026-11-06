package clientcli_test

import (
	"context"
	"strings"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

func TestSteerSendsToTheLeadByDefaultAndSaysWhereItWent(t *testing.T) {
	l := newLive(t, nil)
	run, lead := l.startRun(t, "/work/repo", "alpha")
	w := newWorld(t, l.Listener.Dial)
	l.awaitRun(t, run, hasStatus(pb.RunStatus_RUN_STATUS_RUNNING))

	got := w.run(context.Background(), "run", "steer", run, "try the second plan")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, lead)
	found := false
	for _, s := range l.Sidecars.Sessions() {
		for _, cmd := range s.Commands() {
			found = found || strings.Contains(cmd.String(), "try the second plan")
		}
	}
	if !found {
		t.Error("no session received the steering text")
	}
}

func TestSteerJSONPrintsTheReceiptAndAgentFlagTargetsAnAgent(t *testing.T) {
	f := &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(5, false,
		[]*pb.Run{runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)})},
		dispatch: func(cmd *pb.Command) (*pb.DispatchResponse, error) {
			return &pb.DispatchResponse{Receipt: &pb.Receipt{CommandId: cmd.GetId(), Delivery: pb.Delivery_DELIVERY_QUEUED, MessageId: "m1"}}, nil
		}}
	w := newWorld(t, serveFake(t, f))
	w.dir = "/work/repo"
	got := w.run(context.Background(), "run", "steer", "hello", "--agent", "worker-3", "--json")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, `"messageId":"m1"`, `"delivery":"DELIVERY_QUEUED"`)
	if sent := f.sent(); len(sent) != 1 || sent[0].GetSendMessage().GetAgentId() != "worker-3" || sent[0].GetSendMessage().GetText() != "hello" {
		t.Errorf("sent %v", sent)
	}
}

func TestSteerARejectedMessageNamesTheReason(t *testing.T) {
	rejected := &pb.Message{Id: "m1", RunId: "run-1", ToAgentId: "lead-run-1", Delivery: pb.Delivery_DELIVERY_REJECTED, Rejection: pb.Rejection_REJECTION_RUN_STOPPING, At: at(2),
		Body: &pb.Message_Text{Text: "hello"}}
	f := &fakeService{
		frames: []*pb.WatchRunsResponse{
			snapshotFrame(5, false, []*pb.Run{runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)}),
			messageEvent(6, rejected, 2),
		},
		dispatch: func(cmd *pb.Command) (*pb.DispatchResponse, error) {
			return &pb.DispatchResponse{Receipt: &pb.Receipt{CommandId: cmd.GetId(), Delivery: pb.Delivery_DELIVERY_REJECTED, MessageId: "m1", Sequence: 6}}, nil
		},
	}
	w := newWorld(t, serveFake(t, f))
	got := w.run(context.Background(), "run", "steer", "hello")
	if got.code != 1 {
		t.Fatalf("exit %d, want 1", got.code)
	}
	contains(t, "stderr", got.stderr, "did not take the message", "run stopping")
}

func TestStopIsRefusedForARunThatIsOver(t *testing.T) {
	l := newLive(t, nil)
	run, _ := l.startRun(t, "/work/repo", "alpha")
	w := newWorld(t, l.Listener.Dial)
	if got := w.run(context.Background(), "run", "stop", run, "--yes"); got.code != 0 {
		t.Fatalf("first stop: exit %d, stderr %q", got.code, got.stderr)
	}
	l.awaitRun(t, run, hasStatus(pb.RunStatus_RUN_STATUS_ENDED))
	got := w.run(context.Background(), "run", "stop", run, "--yes")
	if got.code != 1 {
		t.Fatalf("exit %d, want 1; stdout %q", got.code, got.stdout)
	}
	contains(t, "stderr", got.stderr, "refused", "not running")
}

func TestStopEndsTheRunAndTellsHowToWaitForIt(t *testing.T) {
	l := newLive(t, nil)
	run, _ := l.startRun(t, "/work/repo", "alpha")
	w := newWorld(t, l.Listener.Dial)
	got := w.run(context.Background(), "run", "stop", "--yes")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "stopping run "+run, "vibesys-client run wait "+run+" --until ended")
	l.awaitRun(t, run, hasStatus(pb.RunStatus_RUN_STATUS_ENDED))
	state, _ := l.Daemon.State().Run(core.RunID(run))
	if state.Status != core.RunEnded {
		t.Errorf("run is %s", state.Status)
	}
}

// With no terminal nothing is ever asked: stdin is a tripwire that fails the test when read.
func TestStopNeverPromptsWithoutATerminal(t *testing.T) {
	l := newLive(t, nil)
	run, _ := l.startRun(t, "/work/repo", "alpha")
	w := newWorld(t, l.Listener.Dial)
	got := w.run(context.Background(), "run", "stop", run)
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	if strings.Contains(got.stderr, "[y/N]") {
		t.Errorf("prompted: %q", got.stderr)
	}
}

func TestStopAsksOnATerminalUnlessYes(t *testing.T) {
	for _, tc := range []struct {
		name, input string
		args        []string
		code        int
		stopped     bool
	}{
		{"declined", "n\n", nil, 1, false},
		{"empty answer", "\n", nil, 1, false},
		{"accepted", "y\n", nil, 0, true},
		{"yes flag", "", []string{"--yes"}, 0, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			l := newLive(t, nil)
			run, _ := l.startRun(t, "/work/repo", "alpha")
			w := newWorld(t, l.Listener.Dial)
			w.terminal, w.stdin = true, strings.NewReader(tc.input)
			got := w.run(context.Background(), append([]string{"run", "stop", run}, tc.args...)...)
			if got.code != tc.code {
				t.Fatalf("exit %d, want %d; stderr %q", got.code, tc.code, got.stderr)
			}
			if asked := strings.Contains(got.stderr, "[y/N]"); asked == (len(tc.args) > 0) {
				t.Errorf("asked = %v with args %v", asked, tc.args)
			}
			state, _ := l.Daemon.State().Run(core.RunID(run))
			if stopped := state.Status != core.RunRunning; stopped != tc.stopped {
				t.Errorf("stopped = %v, want %v", stopped, tc.stopped)
			}
		})
	}
}
