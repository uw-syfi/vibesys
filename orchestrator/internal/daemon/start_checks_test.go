package daemon_test

import (
	"context"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// A start the checks refuse is answered with the refusal's words and the right code, and leaves
// nothing behind: no run, no event, no receipt. A retry of it is checked again, not remembered.
func TestARefusedStartCreatesNoRunEventOrReceipt(t *testing.T) {
	cases := []struct {
		name    string
		refusal *daemon.StartRefusal
		code    codes.Code
	}{
		{"bad repository or task", &daemon.StartRefusal{Reason: "task \"task\" has no directory /repo/.vibesys/tasks/task"}, codes.InvalidArgument},
		{"no launcher", &daemon.StartRefusal{Reason: "no agent can be launched: no session holder is configured", NotReady: true}, codes.FailedPrecondition},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := daemontest.NewRig(t)
			r.Starts.Refuse(c.refusal)
			last := r.Daemon.Last()
			cmd := startRun("c1")
			for range 2 {
				_, err := r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: cmd})
				if status.Code(err) != c.code || !strings.Contains(err.Error(), c.refusal.Reason) {
					t.Fatalf("got %v, want %v with %q", err, c.code, c.refusal.Reason)
				}
			}
			r.Settle(t)
			if n := len(r.Daemon.State().Runs()); n != 0 || r.Daemon.Last() != last {
				t.Fatalf("%d runs and last event %d (was %d), want none made", n, r.Daemon.Last(), last)
			}
			// Once the cause is fixed the same command id starts the run: nothing was remembered.
			r.Starts.Refuse(nil)
			if receipt := dispatch(t, r, cmd); receipt.GetRunId() == "" {
				t.Fatalf("receipt %v, want a run", receipt)
			}
		})
	}
}

// The check sees exactly the repository and task the command named.
func TestTheStartCheckIsAskedTheRepositoryAndTask(t *testing.T) {
	r := daemontest.NewRig(t)
	dispatch(t, r, startRun("c1"))
	want := []daemontest.StartCall{{Repository: "/repo", Task: "task"}}
	if got := r.Starts.Calls(); len(got) != 1 || got[0] != want[0] {
		t.Fatalf("checks %v, want %v", got, want)
	}
}

// A retry of a start that already has a receipt returns that receipt even when the checks would
// now refuse (the repository was moved, say), and the same id on another command is still the
// conflict it was, not a refusal.
func TestARetryOfAnAcceptedStartStillGetsItsReceiptWhateverTheChecksSayNow(t *testing.T) {
	r := daemontest.NewRig(t)
	first := dispatch(t, r, startRun("c1"))
	r.Starts.Refuse(&daemon.StartRefusal{Reason: "gone"})
	checks := len(r.Starts.Calls())
	again := dispatch(t, r, startRun("c1"))
	if again.GetRunId() != first.GetRunId() || again.GetCommandDigest() != first.GetCommandDigest() {
		t.Fatalf("a retry answered %v, want %v", again, first)
	}
	other := startRun("c1")
	other.GetStartRun().Task = "another"
	if _, err := r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: other}); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("reusing an id for another command gave %v, want AlreadyExists", err)
	}
	if got := len(r.Starts.Calls()); got != checks {
		t.Fatalf("%d checks after the retries, want the %d before them", got, checks)
	}
}

// Only a StartRun from the operator is checked: the other commands, and a StartRun from an agent
// (which Decide refuses with a receipt), never reach the checks.
func TestOnlyTheOperatorsStartRunIsChecked(t *testing.T) {
	r := daemontest.NewRig(t)
	receipt := dispatch(t, r, startRun("c1"))
	r.Settle(t)
	run, _ := r.Daemon.State().Run(core.RunID(receipt.GetRunId()))
	lead, _ := r.Daemon.State().Agent(run.Lead)
	checks := len(r.Starts.Calls())
	r.Starts.Refuse(&daemon.StartRefusal{Reason: "would refuse"})

	ctx := agentContext(lead.ID, lead.Session, r.Auth.Token(core.Session{Agent: lead.ID, N: lead.Session}))
	got, err := r.AgentService.Dispatch(ctx, &pb.DispatchRequest{Command: startRun("agent-start")})
	if err != nil {
		t.Fatalf("an agent's StartRun gave %v, want a receipt that refuses it", err)
	}
	if got.GetReceipt().GetRefusal() == pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("receipt %v, want the agent's start refused by Decide", got.GetReceipt())
	}
	stop := &pb.Command{Id: "stop", Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: receipt.GetRunId()}}}
	if _, err := r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: stop}); err != nil {
		t.Fatalf("a StopRun gave %v, want it unchecked", err)
	}
	if n := len(r.Starts.Calls()); n != checks {
		t.Fatalf("%d checks, want %d: only the operator's StartRun is asked", n, checks)
	}
}
