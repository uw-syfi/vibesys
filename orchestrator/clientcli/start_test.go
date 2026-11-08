package clientcli_test

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protojson"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli"
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

func TestStartOnAnAgentWithoutItsToolsExitsOneNamingTheReason(t *testing.T) {
	l := newLive(t, nil)
	l.Sidecars.Opening = func(core.LaunchSession) []*pb.SessionEvent {
		return []*pb.SessionEvent{{Body: &pb.SessionEvent_Failed{Failed: &pb.SessionFailed{
			Detail:             "could not start the agent: the tool server vibesys did not connect",
			StartFailureReason: pb.StartFailureReason_START_FAILURE_REASON_TOOLS_UNAVAILABLE,
		}}}}
	}
	w := newWorld(t, l.Listener.Dial)
	got := w.run(context.Background(), "run", "start", "--task", "toy")
	if got.code != 1 {
		t.Fatalf("exit %d, want 1; stdout %q stderr %q", got.code, got.stdout, got.stderr)
	}
	contains(t, "stderr", got.stderr, "could not start the agent", "the tool server vibesys did not connect", "tools unavailable")
	if strings.Contains(got.stderr, "sidecar") {
		t.Errorf("stderr names the sidecar: %q", got.stderr)
	}
}

func TestStartRefusalPrintsTheServicesReason(t *testing.T) {
	f := &fakeService{dispatch: func(cmd *pb.Command) (*pb.DispatchResponse, error) {
		return &pb.DispatchResponse{Receipt: &pb.Receipt{CommandId: cmd.GetId(), Refusal: pb.Refusal_REFUSAL_AGENT_NOT_ALLOWED}}, nil
	}}
	w := newWorld(t, serveFake(t, f))
	got := w.run(context.Background(), "run", "start", "--task", "toy")
	if got.code != 1 {
		t.Fatalf("exit %d", got.code)
	}
	contains(t, "stderr", got.stderr, "refused: agent not allowed")
}

func TestStartSendsEveryFlagInTheCommand(t *testing.T) {
	f := &fakeService{dispatch: func(cmd *pb.Command) (*pb.DispatchResponse, error) {
		return &pb.DispatchResponse{Receipt: &pb.Receipt{CommandId: cmd.GetId(), Refusal: pb.Refusal_REFUSAL_RUN_NOT_FOUND}}, nil
	}}
	w := newWorld(t, serveFake(t, f))
	w.dir = "/elsewhere"
	w.run(context.Background(), "run", "start", "--task", "toy", "--repository", "rel/repo", "--brief", "b", "--harness", "codex",
		"--fanout", "7", "--model", "m", "--effort", "high", "--max-time", "90m", "--max-cost", "5.5", "--summary-every", "30m")
	sent := f.sent()
	if len(sent) != 1 {
		t.Fatalf("sent %d commands", len(sent))
	}
	got := sent[0].GetStartRun()
	if got.GetRepository() != "/elsewhere/rel/repo" || got.GetTask() != "toy" || got.GetBrief() != "b" || got.GetFanoutLimit() != 7 ||
		got.GetLead().GetHarness() != pb.Harness_HARNESS_CODEX || got.GetLead().GetModel() != "m" || got.GetLead().GetReasoningEffort() != "high" ||
		got.GetMaxTime().AsDuration() != 90*time.Minute || got.GetMaxCostUsd() != 5.5 || got.GetSummaryEvery().AsDuration() != 30*time.Minute {
		t.Errorf("StartRun = %v", got)
	}
}

// --fanout does not wrap a value too big for uint32 into a small, or even zero, limit: run update's
// --fanout already refuses such a value (see update_test.go), and run start must refuse it the same
// way instead of silently starting a run that can spawn far fewer subagents than asked, or none.
func TestStartFanoutRefusesAValueThatDoesNotFitInThirtyTwoBits(t *testing.T) {
	w := newWorld(t, serveFake(t, &fakeService{}))
	got := w.run(context.Background(), "run", "start", "--task", "toy", "--fanout", "4294967296")
	if got.code != clientcli.ExitUsage {
		t.Fatalf("exit %d, stderr %q, want a usage error", got.code, got.stderr)
	}
	contains(t, "stderr", got.stderr, "4294967296")
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

// Every reason a session can fail to start reaches a person, in the words of its name, on both the
// start that failed and a later look at the run, and reaches a program, by its name.
func TestEveryStartFailureReasonReachesStartAndGet(t *testing.T) {
	for number, name := range pb.StartFailureReason_name {
		reason := pb.StartFailureReason(number)
		if reason == pb.StartFailureReason_START_FAILURE_REASON_UNSPECIFIED {
			continue
		}
		t.Run(name, func(t *testing.T) {
			l := newLive(t, nil)
			l.Sidecars.Opening = func(core.LaunchSession) []*pb.SessionEvent {
				return []*pb.SessionEvent{{Body: &pb.SessionEvent_Failed{Failed: &pb.SessionFailed{
					Detail: "could not start the agent: the detail", StartFailureReason: reason,
				}}}}
			}
			w := newWorld(t, l.Listener.Dial)
			words := strings.ReplaceAll(strings.ToLower(strings.TrimPrefix(name, "START_FAILURE_REASON_")), "_", " ")

			started := w.run(context.Background(), "run", "start", "--task", "toy", "--json")
			if started.code != 1 {
				t.Fatalf("start exited %d, want 1; stdout %q stderr %q", started.code, started.stdout, started.stderr)
			}
			contains(t, "start stderr", started.stderr, "the detail", words)

			var receipt struct {
				RunID string `json:"runId"`
			}
			if err := json.Unmarshal([]byte(started.stdout), &receipt); err != nil || receipt.RunID == "" {
				t.Fatalf("start printed %q: %v", started.stdout, err)
			}
			got := w.run(context.Background(), "run", "get", receipt.RunID)
			contains(t, "get", got.stdout, "the detail", words)
			got = w.run(context.Background(), "run", "get", receipt.RunID, "--json")
			contains(t, "get --json", got.stdout, name)
		})
	}
}

// A run has no limit unless it is given one, and a start without one says so, and how to give it one.
func TestStartWithNoLimitSaysSoAndOneWithALimitDoesNot(t *testing.T) {
	for _, limits := range [][]string{nil, {"--max-time", "30m"}, {"--max-cost", "5"}, {"--max-time", "30m", "--max-cost", "5"}} {
		l := newLive(t, nil)
		w := newWorld(t, l.Listener.Dial)
		got := w.run(context.Background(), append([]string{"run", "start", "--task", "toy", "--json"}, limits...)...)
		if got.code != 0 {
			t.Fatalf("%v: exit %d, stderr %q", limits, got.code, got.stderr)
		}
		said := strings.Contains(got.stderr, "no time or spending limit")
		if said != (len(limits) == 0) {
			t.Errorf("%v: stderr %q", limits, got.stderr)
		}
		if said {
			contains(t, "stderr", got.stderr, "--max-time", "--max-cost")
		}
		for _, r := range l.Daemon.State().Runs() {
			if (len(limits) > 0) != (!r.Deadline.IsZero() || r.MaxCostUSD > 0) {
				t.Errorf("%v: run = %+v", limits, r)
			}
		}
	}
}

// A limit that is not a positive duration or a positive number of dollars is a usage error naming the
// flag, and nothing is sent.
func TestStartRefusesALimitThatIsNotPositive(t *testing.T) {
	f := &fakeService{}
	w := newWorld(t, serveFake(t, f))
	for _, args := range [][]string{
		{"--max-time", "0"}, {"--max-time", "-5m"}, {"--max-time", "soon"}, {"--max-time", "30"},
		{"--max-cost", "0"}, {"--max-cost", "-1"}, {"--max-cost", "abc"}, {"--max-cost", "NaN"}, {"--max-cost", "Inf"},
		{"--summary-every", "0"}, {"--summary-every", "-5m"}, {"--summary-every", "hourly"}, {"--summary-every", "60"},
	} {
		got := w.run(context.Background(), append([]string{"run", "start", "--task", "toy"}, args...)...)
		if got.code != 2 {
			t.Errorf("%v: exit %d, stderr %q", args, got.code, got.stderr)
		}
		contains(t, "stderr", got.stderr, args[0])
	}
	if sent := f.sent(); len(sent) != 0 {
		t.Errorf("sent %d commands", len(sent))
	}
}

// The usage text is where a person finds what a limit does, including what the cost can and cannot say.
func TestTheUsageSaysWhatALimitDoes(t *testing.T) {
	contains(t, "usage", clientcli.Usage, "--max-time", "--max-cost", "told once to finish", "10 minutes", "what is in flight", "reports no cost adds nothing",
		"--summary-every", "progress report")
}
