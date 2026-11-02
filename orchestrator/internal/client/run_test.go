package client_test

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/mcp"
)

// guard bounds a wait that only a hang would make long.
const guard = 30 * time.Second

// feed reads a stream of runs in the background, so a test can wait for an event by predicate.
type feed struct {
	t      testing.TB
	events chan *pb.Event
	snap   chan *pb.RunsSnapshot
	// seen is every event read so far, so that waiting for one never loses another.
	seen []*pb.Event
}

func watchRuns(ctx context.Context, t testing.TB, c *client.Client, after uint64) *feed {
	t.Helper()
	stream, err := c.WatchRuns(ctx, after)
	if err != nil {
		t.Fatal(err)
	}
	f := &feed{t: t, events: make(chan *pb.Event, 4096), snap: make(chan *pb.RunsSnapshot, 16)}
	go func() {
		var _ grpc.ServerStreamingClient[pb.WatchRunsResponse] = stream
		for {
			msg, err := stream.Recv()
			if err != nil {
				close(f.events)
				return
			}
			if s := msg.GetSnapshot(); s != nil {
				f.snap <- s
			} else {
				f.events <- msg.GetEvent()
			}
		}
	}()
	return f
}

// until returns the first event, already read or still to come, that satisfies the predicate.
func (f *feed) until(what string, match func(*pb.Event) bool) *pb.Event {
	f.t.Helper()
	for _, ev := range f.seen {
		if match(ev) {
			return ev
		}
	}
	timeout := time.After(guard)
	for {
		select {
		case ev, ok := <-f.events:
			if !ok {
				f.t.Fatalf("the stream ended while waiting for %s", what)
			}
			f.seen = append(f.seen, ev)
			if match(ev) {
				return ev
			}
		case <-timeout:
			f.t.Fatalf("no event for %s", what)
		}
	}
}

func turnDone(agent string, ordinal uint32) func(*pb.Event) bool {
	return func(ev *pb.Event) bool {
		t := ev.GetTurnUpdated()
		return t != nil && t.GetAgentId() == agent && t.GetOrdinal() == ordinal && t.GetStatus() == pb.TurnStatus_TURN_STATUS_COMPLETED
	}
}

func toolClient(ctx context.Context, t testing.TB, live *daemontest.Live, agent core.AgentID, role core.AgentRole, run core.RunID) (*mcp.Server, *client.Client) {
	t.Helper()
	var session *daemontest.Session
	for _, s := range live.Sidecars.Sessions() {
		if s.Launch.Session.Agent == agent {
			session = s
		}
	}
	if session == nil {
		t.Fatalf("agent %s has no session", agent)
	}
	var n uint32
	fmt.Sscan(session.Env[daemon.EnvSession], &n)
	c, err := client.Dial(ctx, live.Listener.Dial, client.Options{Session: &client.Session{
		Agent: session.Env[daemon.EnvAgent], Session: n, Token: session.Env[daemon.EnvToken],
	}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	return mcp.New(mcp.Identity{Agent: agent, Run: run, Role: role}, c, "proc-"+string(agent)), c
}

func callTool(t testing.TB, s *mcp.Server, id int, name string, args map[string]any) string {
	t.Helper()
	raw, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": map[string]any{"name": name, "arguments": args}})
	var resp struct {
		Result struct {
			IsError bool `json:"isError"`
			Content []struct{ Text string }
		}
	}
	if err := json.Unmarshal(s.Handle(context.Background(), raw), &resp); err != nil {
		t.Fatal(err)
	}
	text := resp.Result.Content[0].Text
	if resp.Result.IsError {
		t.Fatalf("%s failed: %s", name, text)
	}
	return text
}

// TestAWholeRunFromAClient drives one run through the public service API alone: the operator starts
// it, the lead spawns a worker, the worker hands off, the lead wakes and reports, and the operator
// stops the run.
func TestAWholeRunFromAClient(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*guard)
	defer cancel()
	rig := daemontest.NewRig(t)
	rig.Sidecars.React = func(s *daemontest.Session, cmd *pb.SidecarCommand) {
		if _, turn := cmd.GetBody().(*pb.SidecarCommand_StartTurn); turn && s.Launch.Role == core.RoleWorker {
			return // a worker keeps working until the test says it is done.
		}
		daemontest.DefaultReaction(s, cmd)
	}
	live := rig.Serve(t)
	op, err := client.Dial(ctx, live.Listener.Dial, client.Options{BinaryVersion: "test"})
	if err != nil {
		t.Fatal(err)
	}
	defer op.Close()
	feed := watchRuns(ctx, t, op, 0)
	<-feed.snap

	// The operator starts the run.
	receipt, err := op.Dispatch(ctx, &pb.Command{Id: "start-1", Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
		Repository: "/repo", Task: "task", FanoutLimit: 2, Brief: "begin",
		Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
	}}})
	if err != nil || receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("start: %v %v", receipt, err)
	}
	run := core.RunID(receipt.GetRunId())
	state, _ := rig.Daemon.State().Run(run)
	lead := string(state.Lead)
	feed.until("the lead's first turn", turnDone(lead, 1))

	// The lead spawns a worker through its tools.
	leadTools, _ := toolClient(ctx, t, live, core.AgentID(lead), core.RoleLead, run)
	spawned := callTool(t, leadTools, 1, "spawn_agent", map[string]any{"role": "worker", "brief": "try plan 1", "plan_number": 1})
	worker := strings.TrimSuffix(strings.TrimPrefix(spawned, "started agent "), ". It will post a handoff when it is done; list_agents shows where it stands.")
	feed.until("the worker's turn to start", func(ev *pb.Event) bool {
		t := ev.GetTurnUpdated()
		return t != nil && t.GetAgentId() == worker && t.GetStatus() == pb.TurnStatus_TURN_STATUS_RUNNING
	})
	if listing := callTool(t, leadTools, 2, "list_agents", nil); !strings.Contains(listing, worker) {
		t.Fatalf("list_agents does not show the worker %s: %s", worker, listing)
	}

	// The worker hands off; the lead wakes for it.
	workerTools, _ := toolClient(ctx, t, live, core.AgentID(worker), core.RoleWorker, run)
	callTool(t, workerTools, 1, "post_handoff", map[string]any{"plan_number": 1, "claim": "plan 1 is 3% faster", "has_candidate": true, "commit": "abc123"})
	feed.until("the lead to wake for the handoff", turnDone(lead, 2))
	workerSession := rig.Sidecars.Session(core.Session{Agent: core.AgentID(worker), N: 1})
	var turnID string
	for _, cmd := range workerSession.Received {
		if st := cmd.GetStartTurn(); st != nil {
			turnID = st.GetTurnId()
		}
	}
	workerSession.EmitEvents(&pb.SidecarEvent{Body: &pb.SidecarEvent_TurnEnded{TurnEnded: &pb.TurnEnded{TurnId: turnID, Status: pb.TurnStatus_TURN_STATUS_COMPLETED}}})
	feed.until("the worker's session to end", func(ev *pb.Event) bool {
		a := ev.GetAgentUpdated()
		return a != nil && a.GetId() == worker && a.GetStatus() == pb.AgentStatus_AGENT_STATUS_ENDED
	})

	// The lead reports; the operator sees it.
	callTool(t, leadTools, 3, "post_report", map[string]any{"headline": "plan 1 won"})
	feed.until("the report", func(ev *pb.Event) bool {
		return ev.GetMessageUpdated().GetReport().GetHeadline() == "plan 1 won"
	})

	// The operator stops the run.
	if r, err := op.Dispatch(ctx, &pb.Command{Id: "stop-1", Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(run)}}}); err != nil || r.GetRefusal() != 0 {
		t.Fatalf("stop: %v %v", r, err)
	}
	feed.until("the run to end", func(ev *pb.Event) bool {
		r := ev.GetRunUpdated()
		return r != nil && r.GetId() == string(run) && r.GetStatus() == pb.RunStatus_RUN_STATUS_ENDED
	})
}
