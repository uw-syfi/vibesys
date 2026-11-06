package daemon_test

import (
	"context"
	"errors"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

func toolA() core.JobToolEndpoint {
	return core.JobToolEndpoint{Executable: "/opt/a/vibesys-cli", RuntimeDirectory: "/run/user/1/vibesys-cli"}
}

func toolB() core.JobToolEndpoint {
	return core.JobToolEndpoint{Executable: "/opt/b/vibesys-cli", RuntimeDirectory: "/run/user/2/vibesys-cli"}
}

// The vibesys-cli an agent runs, and the daemon it talks to, are the ones recorded when the run
// started: every session of the run is launched with them, also a session resumed after the
// daemon restarted with another environment, and a run that starts later records the new ones.
func TestEverySessionOfARunIsLaunchedWithTheJobToolItRecorded(t *testing.T) {
	r := daemontest.NewRig(t)
	r.JobTool = toolA()
	r.Restart(t)
	first, lead := beginRun(t, r)
	if first.JobTool != toolA() {
		t.Fatalf("the run recorded %+v, want %+v", first.JobTool, toolA())
	}
	r.Settle(t)
	r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1}).Die()
	r.JobTool = toolB()
	r.Restart(t)
	r.Settle(t)
	sessions := r.Sidecars.Sessions()
	if len(sessions) != 2 {
		t.Fatalf("%d sessions, want the lead's two", len(sessions))
	}
	for _, s := range sessions {
		got := s.Start.JobTool
		if got != toolA() || s.Start.Task != first.Task {
			t.Errorf("session %s/%d was launched with %+v for task %q, want %+v for %q",
				s.Launch.Session.Agent, s.Launch.Session.N, got, s.Start.Task, toolA(), first.Task)
		}
	}
}

// A daemon with no job tool to give records none, and the session is launched with none: the
// skills say what to do then, and a daemon never hands out a guess.
func TestARunStartedWithoutAJobToolRecordsNone(t *testing.T) {
	r := daemontest.NewRig(t)
	run, lead := beginRun(t, r)
	if run.JobTool != (core.JobToolEndpoint{}) {
		t.Fatalf("the run recorded %+v with none given", run.JobTool)
	}
	if got := r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1}).Start.JobTool; got != (core.JobToolEndpoint{}) {
		t.Fatalf("the session was launched with %+v with none recorded", got)
	}
}

// The tool's answer is asked for before the engine is, and a start it cannot answer is refused like
// any start that cannot work: no run and no receipt, the tool's own words, and the same command works
// once the tool can answer.
func TestAStartTheJobToolCannotAnswerIsRefusedAndMakesNoRun(t *testing.T) {
	r := daemontest.NewRig(t)
	r.JobToolErr = errors.New("the daemon that runs has 2 jobs")
	start := &pb.DispatchRequest{Command: &pb.Command{Id: "start", Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
		Repository: "/repo", Task: "task", FanoutLimit: 1, Brief: "go", Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
	}}}}
	_, err := r.Service.Dispatch(context.Background(), start)
	if status.Code(err) != codes.FailedPrecondition || !strings.Contains(err.Error(), "has 2 jobs") {
		t.Fatalf("the start got %v", err)
	}
	if runs := r.Daemon.State().Runs(); len(runs) != 0 {
		t.Fatalf("a refused start made %d runs", len(runs))
	}
	r.JobToolErr = nil
	r.JobTool = toolA()
	if _, err := r.Service.Dispatch(context.Background(), start); err != nil {
		t.Fatalf("the same command once the tool answers: %v", err)
	}
	r.Settle(t)
	for _, run := range r.Daemon.State().Runs() {
		if run.JobTool != toolA() {
			t.Fatalf("the run records %+v", run.JobTool)
		}
	}
}
