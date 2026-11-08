package clientcli_test

import (
	"context"
	"testing"
	"time"

	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// updateFake serves a running run with a lead on claude_code opus, answers an update with a receipt, and
// streams the run as the update leaves it (at sequence 6), or unchanged when after is nil.
func updateFake(after func(*pb.Run), refusal pb.Refusal) *fakeService {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)
	lead.Model, lead.ReasoningEffort = "opus", "high"
	frames := []*pb.WatchRunsResponse{snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{lead})}
	sequence := uint64(5)
	if after != nil {
		changed := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
		after(changed)
		frames = append(frames, runEvent(6, changed, 2))
		sequence = 6
	}
	return &fakeService{frames: frames, dispatch: func(cmd *pb.Command) (*pb.DispatchResponse, error) {
		return &pb.DispatchResponse{Receipt: &pb.Receipt{CommandId: cmd.GetId(), Refusal: refusal, RunId: "run-1", Sequence: sequence}}, nil
	}}
}

func TestUpdateSendsOnlyTheSettingsNamedAndSaysWhatChanged(t *testing.T) {
	cost := 5.0
	f := updateFake(func(r *pb.Run) {
		r.MaxCostUsd, r.FanoutLimit = &cost, 1
		r.Deadline = timestamppb.New(r.GetCreatedAt().AsTime().Add(3 * time.Hour))
	}, pb.Refusal_REFUSAL_UNSPECIFIED)
	w := newWorld(t, serveFake(t, f))
	got := w.run(context.Background(), "run", "update", "--max-cost", "5", "--max-time", "3h", "--fanout", "1")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "run run-1: max cost none -> $5.00", "max time none -> 3h0m0s", "fanout 2 -> 1, and the agents already live keep going")
	sent := f.sent()
	if len(sent) != 1 {
		t.Fatalf("sent %v", sent)
	}
	u := sent[0].GetUpdateRun()
	if u.GetRunId() != "run-1" || u.GetCostLimit().GetSetUsd() != 5 || u.GetTimeLimit().GetSet().AsDuration() != 3*time.Hour || u.GetFanoutLimit() != 1 || u.Lead != nil {
		t.Errorf("sent %v", u)
	}
}

func TestUpdateNoneRemovesALimit(t *testing.T) {
	f := updateFake(nil, pb.Refusal_REFUSAL_UNSPECIFIED)
	w := newWorld(t, serveFake(t, f))
	got := w.run(context.Background(), "run", "update", "--max-cost", "none", "--max-time", "NONE")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	u := f.sent()[0].GetUpdateRun()
	if !u.GetCostLimit().GetRemove() || !u.GetTimeLimit().GetRemove() || u.FanoutLimit != nil {
		t.Errorf("sent %v", u)
	}
	// The view the answer is read from shows no change, so the answer says so.
	contains(t, "stdout", got.stdout, "nothing changed")
}

// A lead flag leaves out what the lead already has, unless the tool changes: another tool has its own
// default model and effort.
func TestUpdateAsksForTheLeadFromTheLeadItHas(t *testing.T) {
	for name, tc := range map[string]struct {
		args []string
		want *pb.AgentSpec
	}{
		"model only keeps the tool and the effort":  {[]string{"--model", "sonnet"}, &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE, Model: "sonnet", ReasoningEffort: "high"}},
		"effort only keeps the tool and the model":  {[]string{"--effort", "low"}, &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE, Model: "opus", ReasoningEffort: "low"}},
		"another tool starts from its own defaults": {[]string{"--harness", "codex"}, &pb.AgentSpec{Harness: pb.Harness_HARNESS_CODEX}},
		"another tool and a model":                  {[]string{"--harness", "codex", "--model", "gpt"}, &pb.AgentSpec{Harness: pb.Harness_HARNESS_CODEX, Model: "gpt"}},
		"the same tool keeps the rest":              {[]string{"--harness", "claude_code"}, &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE, Model: "opus", ReasoningEffort: "high"}},
	} {
		t.Run(name, func(t *testing.T) {
			f := updateFake(func(r *pb.Run) {
				r.PendingLead = &pb.PendingLead{Harness: tc.want.GetHarness(), Model: tc.want.GetModel(), ReasoningEffort: tc.want.GetReasoningEffort()}
			}, pb.Refusal_REFUSAL_UNSPECIFIED)
			w := newWorld(t, serveFake(t, f))
			args := append([]string{"run", "update"}, tc.args...)
			got := w.run(context.Background(), args...)
			if got.code != 0 {
				t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
			}
			lead := f.sent()[0].GetUpdateRun().GetLead()
			if lead.GetHarness() != tc.want.GetHarness() || lead.GetModel() != tc.want.GetModel() || lead.GetReasoningEffort() != tc.want.GetReasoningEffort() {
				t.Errorf("asked for lead %v, want %v", lead, tc.want)
			}
			contains(t, "stdout", got.stdout, "lead claude_code opus -> ", "once its current turn ends")
		})
	}
}

func TestUpdateRefusalsSayWhy(t *testing.T) {
	for refusal, want := range map[pb.Refusal]string{
		pb.Refusal_REFUSAL_COST_LIMIT_REACHED: "reached its cost limit",
		pb.Refusal_REFUSAL_TIME_LIMIT_REACHED: "reached its time limit",
		pb.Refusal_REFUSAL_RUN_NOT_RUNNING:    "run not running",
	} {
		w := newWorld(t, serveFake(t, updateFake(nil, refusal)))
		got := w.run(context.Background(), "run", "update", "--max-cost", "9")
		if got.code != 1 {
			t.Fatalf("%s: exit %d, want 1", refusal, got.code)
		}
		contains(t, "stderr", got.stderr, "refused", want)
	}
}

func TestUpdateOfAModelTheHostForbidsNamesTheAllowedOnes(t *testing.T) {
	f := &fakeService{
		frames: []*pb.WatchRunsResponse{snapshotFrame(5, false, []*pb.Run{runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)})},
		dispatch: func(cmd *pb.Command) (*pb.DispatchResponse, error) {
			return &pb.DispatchResponse{Receipt: &pb.Receipt{CommandId: cmd.GetId(), Refusal: pb.Refusal_REFUSAL_MODEL_NOT_ALLOWED, Model: "gpt", AllowedModels: []string{"opus", "sonnet"}}}, nil
		},
	}
	got := newWorld(t, serveFake(t, f)).run(context.Background(), "run", "update", "--harness", "codex", "--model", "gpt")
	if got.code != 1 {
		t.Fatalf("exit %d", got.code)
	}
	contains(t, "stderr", got.stderr, "does not allow gpt", "opus, sonnet")
}

func TestUpdateNeedsASettingAndValidOnes(t *testing.T) {
	f := updateFake(nil, pb.Refusal_REFUSAL_UNSPECIFIED)
	w := newWorld(t, serveFake(t, f))
	for name, args := range map[string][]string{
		"no setting":            {"run", "update"},
		"only a run":            {"run", "update", "run-1"},
		"a fanout of none":      {"run", "update", "--fanout", "0"},
		"a fanout that is text": {"run", "update", "--fanout", "many"},
		"a free run of dollars": {"run", "update", "--max-cost", "0"},
		"dollars that are text": {"run", "update", "--max-cost", "lots"},
		"a negative time":       {"run", "update", "--max-time", "-1h"},
		"a time with no unit":   {"run", "update", "--max-time", "30"},
		"a tool that is not":    {"run", "update", "--harness", "nope"},
		"two runs":              {"run", "update", "run-1", "run-2", "--fanout", "1"},
	} {
		if got := w.run(context.Background(), args...); got.code != 2 {
			t.Errorf("%s: exit %d, stderr %q, want a usage error", name, got.code, got.stderr)
		}
	}
	if sent := f.sent(); len(sent) != 0 {
		t.Errorf("%d commands were sent for arguments that were refused", len(sent))
	}
}

func TestUpdateOfARunThatIsOverSaysSoAndSendsNothing(t *testing.T) {
	ended := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_ENDED, 0)
	ended.EndedAt = at(4)
	f := &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(5, false, []*pb.Run{ended}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ENDED)})}}
	got := newWorld(t, serveFake(t, f)).run(context.Background(), "run", "update", "--fanout", "1")
	if got.code != 1 || len(f.sent()) != 0 {
		t.Fatalf("exit %d, %d commands sent", got.code, len(f.sent()))
	}
	contains(t, "stderr", got.stderr, "already", "nothing to change")
}

func TestUpdateJSONPrintsTheReceipt(t *testing.T) {
	w := newWorld(t, serveFake(t, updateFake(nil, pb.Refusal_REFUSAL_UNSPECIFIED)))
	got := w.run(context.Background(), "run", "update", "--fanout", "3", "--json")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, `"runId":"run-1"`, `"sequence":"5"`)
}

// Against the real core: the limits and the fan-out change at once, and the lead's change is asked for
// and waits.
func TestUpdateChangesALiveRun(t *testing.T) {
	l := newLive(t, nil)
	run, _ := l.startRun(t, "/work/repo", "alpha")
	w := newWorld(t, l.Listener.Dial)

	got := w.run(context.Background(), "run", "update", "--max-cost", "7", "--max-time", "2h", "--fanout", "1")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "max cost none -> $7.00", "max time none -> 2h0m0s", "fanout 2 -> 1")
	state, _ := l.Daemon.State().Run(core.RunID(run))
	if state.MaxCostUSD != 7 || state.FanoutLimit != 1 || state.Deadline.Sub(state.CreatedAt) != 2*time.Hour {
		t.Errorf("the run is %+v", state)
	}

	got = w.run(context.Background(), "run", "update", "--harness", "codex", "--model", "gpt")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "lead claude_code -> codex gpt, once its current turn ends")

	// A limit removed.
	got = w.run(context.Background(), "run", "update", "--max-cost", "none", "--fanout", "1")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "max cost $7.00 -> none")
	if state, _ := l.Daemon.State().Run(core.RunID(run)); state.MaxCostUSD != 0 {
		t.Errorf("the cost limit is %v after it was removed", state.MaxCostUSD)
	}
}
