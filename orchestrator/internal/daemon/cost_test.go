package daemon_test

import (
	"context"
	"fmt"
	"math"
	"math/rand/v2"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
)

// costWorld is a run whose agents answer each turn with the usage a test chose, and a watcher of
// the run's cost.
type costWorld struct {
	t       *testing.T
	r       *daemontest.Rig
	run     core.Run
	agents  []core.AgentID
	next    func(agent core.AgentID) *pb.Usage
	watch   *daemon.CostWatch
	current *pb.RunCost
}

func newCostWorld(t *testing.T, limits hub.Limits) *costWorld {
	t.Helper()
	r := daemontest.NewRig(t)
	if limits.MaxItems > 0 {
		r.Limits = limits
		r.Restart(t)
	}
	run, lead := beginRun(t, r)
	w := &costWorld{t: t, r: r, run: run, agents: []core.AgentID{lead.ID}}
	r.Sidecars.React = func(s *daemontest.Session, cmd *pb.SessionCommand) {
		turn, ok := cmd.GetBody().(*pb.SessionCommand_StartTurn)
		if !ok {
			daemontest.DefaultReaction(s, cmd)
			return
		}
		id := turn.StartTurn.GetTurnId()
		s.Emit(&pb.SessionEvent{Body: &pb.SessionEvent_TurnEnded{TurnEnded: &pb.TurnEnded{
			TurnId: id, Status: pb.TurnStatus_TURN_STATUS_COMPLETED, Usage: w.next(s.Launch.Session.Agent),
		}}})
	}
	w.next = func(core.AgentID) *pb.Usage { return nil }
	return w
}

// spawn has the lead start a worker, which runs its first turn at once.
func (w *costWorld) spawn(id string) core.AgentID {
	w.t.Helper()
	token := w.r.Sidecars.Session(core.Session{Agent: w.agents[0], N: 1}).Credentials.GetToken()
	resp, err := w.r.AgentService.Dispatch(agentContext(w.agents[0], 1, token), &pb.DispatchRequest{Command: &pb.Command{Id: id, Body: &pb.Command_SpawnAgent{SpawnAgent: &pb.SpawnAgent{
		RunId: string(w.run.ID), Role: pb.AgentRole_AGENT_ROLE_WORKER, Brief: "work",
	}}}})
	if err != nil || resp.GetReceipt().GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		w.t.Fatalf("spawn: %v, %v", resp, err)
	}
	w.r.Settle(w.t)
	agent := core.AgentID(resp.GetReceipt().GetAgentId())
	w.agents = append(w.agents, agent)
	return agent
}

// ask puts a message to an agent, which starts a turn that ends with the usage next gives.
func (w *costWorld) ask(agent core.AgentID, id string) {
	w.t.Helper()
	if _, err := w.r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{
		Id: id, Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(agent), Text: "again"}},
	}}); err != nil {
		w.t.Fatal(err)
	}
	w.r.Settle(w.t)
}

func (w *costWorld) open() []*pb.WatchRunCostResponse {
	w.t.Helper()
	var first []*pb.WatchRunCostResponse
	var err error
	w.watch, first, err = w.r.Service.WatchRunCostFrom(w.run.ID)
	if err != nil {
		w.t.Fatal(err)
	}
	w.t.Cleanup(w.watch.Close)
	w.current = first[len(first)-1].GetCost()
	return first
}

// poll reads what the stream has ready and keeps the last view, as a client does.
func (w *costWorld) poll() []*pb.WatchRunCostResponse {
	w.t.Helper()
	got, err := w.watch.Poll()
	if err != nil {
		w.t.Fatal(err)
	}
	if len(got) > 0 {
		w.current = got[len(got)-1].GetCost()
	}
	return got
}

func (w *costWorld) fresh() *pb.RunCost {
	w.t.Helper()
	again, first, err := w.r.Service.WatchRunCostFrom(w.run.ID)
	if err != nil {
		w.t.Fatal(err)
	}
	defer again.Close()
	return first[0].GetCost()
}

func dollars(c float64) *pb.Usage { return &pb.Usage{CostUsd: &c} }

func costOf(u core.Usage) float64 {
	if u.CostUSD == nil {
		return 0
	}
	return *u.CostUSD
}

func approx(a, b float64) bool { return math.Abs(a-b) <= 1e-9*(1+math.Abs(a)+math.Abs(b)) }

// The first view of a run holds what its finished turns cost, which is what the run's own usage
// says, split by type of agent and by model, and the view a client keeps after every turn that
// ends is the view a client that connects then is given.
func TestCostViewTracksTheRunAsTurnsEnd(t *testing.T) {
	w := newCostWorld(t, hub.Limits{})
	w.next = func(core.AgentID) *pb.Usage { return dollars(0.25) }
	worker := w.spawn("spawn-1")
	first := w.open()
	if len(first) != 1 {
		t.Fatalf("%d responses at the start, want the one view", len(first))
	}
	check := func(when string) {
		t.Helper()
		run, _ := w.r.Daemon.State().Run(w.run.ID)
		got := w.current
		if got.GetTotal().GetCostUsd() == 0 || !approx(got.GetTotal().GetCostUsd(), costOf(run.Usage)) {
			t.Fatalf("%s: view total %v, run usage %v", when, got.GetTotal().GetCostUsd(), run.Usage.CostUSD)
		}
		var roles uint32
		for _, r := range got.Roles {
			roles += r.Agents
		}
		if roles != uint32(len(w.agents)) {
			t.Fatalf("%s: types hold %d agents, run has %d", when, roles, len(w.agents))
		}
		if !proto.Equal(got, w.fresh()) {
			t.Fatalf("%s: the view a client keeps differs from a new client's", when)
		}
	}
	check("at the start")
	for i := range 5 {
		w.ask([]core.AgentID{w.agents[0], worker}[i%2], fmt.Sprint("ask-", i))
		w.poll()
		check(fmt.Sprint("after turn ", i+1))
	}
	if got := w.current.GetTurns(); got < 5 {
		t.Fatalf("%d turns counted, want at least the 5 asked for", got)
	}
}

// A turn that reported tokens and no price is counted, with its tokens, and adds no dollar: the view
// says how many turns are unpriced and the total stays what the priced turns cost.
func TestCostViewNeverPricesATurnTheProviderDidNot(t *testing.T) {
	w := newCostWorld(t, hub.Limits{})
	w.open()
	tokens := uint64(1000)
	w.next = func(core.AgentID) *pb.Usage { return &pb.Usage{InputTokens: &tokens} }
	w.ask(w.agents[0], "ask-1")
	w.poll()
	got := w.current
	if got.Total.CostUsd != nil || got.LastHourUsd != nil || got.UnpricedTurns != 1 || got.Total.GetInputTokens() != tokens {
		t.Fatalf("an unpriced turn: %v", got)
	}
	w.next = func(core.AgentID) *pb.Usage { return dollars(0.5) }
	w.ask(w.agents[0], "ask-2")
	w.poll()
	if got = w.current; got.GetTotal().GetCostUsd() != 0.5 || got.UnpricedTurns != 1 || got.Turns != 3 {
		t.Fatalf("after a priced turn: %v", got)
	}
	if lead := got.Roles[0]; lead.UnpricedTurns != 1 || lead.Usage.GetCostUsd() != 0.5 {
		t.Fatalf("the type holds %v", lead)
	}
}

// The events of one poll make one view, not one for each turn that ended.
func TestCostViewIsOneFramePerPoll(t *testing.T) {
	w := newCostWorld(t, hub.Limits{})
	w.next = func(core.AgentID) *pb.Usage { return dollars(1) }
	worker := w.spawn("spawn-1")
	w.open()
	for i := range 4 {
		w.ask([]core.AgentID{w.agents[0], worker}[i%2], fmt.Sprint("ask-", i))
	}
	got := w.poll()
	if len(got) != 1 {
		t.Fatalf("%d frames for the events of four turns, want one", len(got))
	}
	if again := w.poll(); len(again) != 0 {
		t.Fatalf("a poll with nothing new sent %d frames", len(again))
	}
}

// A new agent counts as one of its type as soon as it exists, before any of its turns ends.
func TestCostViewCountsANewAgentBeforeItsFirstTurn(t *testing.T) {
	w := newCostWorld(t, hub.Limits{})
	w.next = func(core.AgentID) *pb.Usage { return dollars(1) }
	w.open()
	w.r.Sidecars.Opening = func(core.LaunchSession) []*pb.SessionEvent { return nil } // the worker's session never opens.
	w.spawn("spawn-1")
	w.poll()
	var workers uint32
	for _, r := range w.current.Roles {
		if r.Role == pb.AgentRole_AGENT_ROLE_WORKER {
			workers = r.Agents
		}
	}
	if workers != 1 {
		t.Fatalf("the view holds %d workers, want the one that was spawned: %v", workers, w.current)
	}
}

// A run that is live is worked out again as time passes with nothing happening in it, so a rate
// does not stand still, and a run that is over has one last view that no later time changes.
func TestCostViewFollowsTheClockWhileTheRunLivesAndStopsWhenItEnds(t *testing.T) {
	w := newCostWorld(t, hub.Limits{})
	w.next = func(core.AgentID) *pb.Usage { return dollars(2) }
	w.ask(w.agents[0], "ask-1")
	w.open()
	if got := w.poll(); len(got) != 0 {
		t.Fatalf("a poll right after the first view sent %v", got)
	}
	if w.current.GetLastHourUsd() != 2 {
		t.Fatalf("last hour %v, want the turn's 2", w.current.GetLastHourUsd())
	}
	w.r.Clock.Advance(daemon.CostRefresh / 2)
	if got := w.poll(); len(got) != 0 {
		t.Fatalf("a view was sent before the refresh was due: %v", got)
	}
	w.r.Clock.Advance(daemon.CostRefresh)
	if got := w.poll(); len(got) != 1 {
		t.Fatalf("%d views once the refresh was due, want one", len(got))
	}
	w.r.Clock.Advance(2 * time.Hour)
	w.poll()
	if got := w.current.GetLastHourUsd(); got != 0 {
		t.Fatalf("two hours on with no turn, last hour is %v, want 0", got)
	}
	if _, err := w.r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{
		Id: "stop", Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(w.run.ID)}},
	}}); err != nil {
		t.Fatal(err)
	}
	w.r.Settle(t)
	w.poll()
	ended := w.current
	if run, _ := w.r.Daemon.State().Run(w.run.ID); !run.Status.Final() || !ended.AsOf.AsTime().Equal(run.EndedAt) {
		t.Fatalf("run %s ended %v, last view is as of %v", run.Status, run.EndedAt, ended.AsOf.AsTime())
	}
	w.r.Clock.Advance(3 * daemon.CostRefresh)
	if got := w.poll(); len(got) != 0 {
		t.Fatalf("a view of an ended run changed with the clock: %v", got)
	}
}

// A watcher that fell behind its bounds is given a whole view again, and it is the current one.
func TestCostViewResyncsAWatcherThatFellBehind(t *testing.T) {
	w := newCostWorld(t, hub.Limits{MaxItems: 2, MaxBytes: 1 << 20})
	w.next = func(core.AgentID) *pb.Usage { return dollars(1) }
	w.open()
	for i := range 6 {
		w.ask(w.agents[0], fmt.Sprint("ask-", i))
	}
	w.poll()
	if !proto.Equal(w.current, w.fresh()) {
		t.Fatalf("after falling behind the watcher holds %v, a new one %v", w.current, w.fresh())
	}
}

// Whatever order turns end in, however often the stream is read, and whether the watcher falls behind,
// the view a watcher holds is the view a new watcher is given, and its total is the run's usage.
func TestCostViewConvergesWhateverTheScript(t *testing.T) {
	for seed := range uint64(40) {
		rnd := rand.New(rand.NewPCG(seed, 5))
		w := newCostWorld(t, hub.Limits{MaxItems: 1 + rnd.IntN(8), MaxBytes: 1 << 20})
		w.next = func(core.AgentID) *pb.Usage {
			switch rnd.IntN(4) {
			case 0:
				return nil
			case 1:
				n := uint64(rnd.IntN(500) + 1)
				return &pb.Usage{OutputTokens: &n}
			}
			c, n := float64(rnd.IntN(2048))/1024, uint64(rnd.IntN(500))
			return &pb.Usage{CostUsd: &c, InputTokens: &n}
		}
		w.spawn("spawn-1")
		w.open()
		for i := range 30 {
			switch rnd.IntN(8) {
			case 0:
				w.poll()
			case 1:
				w.r.Clock.Advance(time.Duration(rnd.IntN(150)) * time.Minute)
			default:
				w.ask(w.agents[rnd.IntN(len(w.agents))], fmt.Sprint("ask-", i))
			}
		}
		for len(w.poll()) > 0 {
		}
		fresh := w.fresh()
		if !proto.Equal(w.current, fresh) {
			t.Fatalf("seed %d: the watcher holds\n%v\na new one is given\n%v", seed, w.current, fresh)
		}
		run, _ := w.r.Daemon.State().Run(w.run.ID)
		if got := fresh.GetTotal().GetCostUsd(); !approx(got, costOf(run.Usage)) {
			t.Fatalf("seed %d: total %v, run usage %v", seed, got, costOf(run.Usage))
		}
		window, err := w.r.Store.Turns(w.run.ID, time.Time{}, time.Date(9999, 1, 1, 0, 0, 0, 0, time.UTC), store.MaxWindowTurns)
		if err != nil {
			t.Fatal(err)
		}
		var finished uint32
		for _, turn := range window.Turns {
			if turn.Status != core.TurnRunning {
				finished++
			}
		}
		if fresh.Turns != finished {
			t.Fatalf("seed %d: %d turns counted, the store has %d finished", seed, fresh.Turns, finished)
		}
	}
}

// A view is read from the store in as many windows as the run's turns need, however many that is, and
// counts every one of them.
func TestCostViewCountsMoreTurnsThanAWindowHolds(t *testing.T) {
	w := newCostWorld(t, hub.Limits{MaxItems: 1 << 20, MaxBytes: 1 << 30})
	w.next = func(core.AgentID) *pb.Usage { return dollars(0.5) }
	const turns = store.MaxWindowTurns + 300
	for i := range turns {
		w.ask(w.agents[0], fmt.Sprint("ask-", i))
		w.r.Clock.Advance(time.Second)
	}
	got := w.fresh()
	if got.Turns != turns+1 || !approx(got.GetTotal().GetCostUsd(), 0.5*turns) {
		t.Fatalf("%d turns and %v dollars, want the %d asked for at 0.5 and the lead's first, which reported nothing", got.Turns, got.GetTotal().GetCostUsd(), turns)
	}
}

// Only the operator reads a run's cost, and a run that does not exist is not found.
func TestCostIsForTheOperatorAndForRunsThatExist(t *testing.T) {
	r := daemontest.NewRig(t)
	run, lead := beginRun(t, r)
	if _, _, err := r.Service.WatchRunCostFrom("nope"); status.Code(err) != codes.NotFound {
		t.Fatalf("a run that does not exist: %v", err)
	}
	token := r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1}).Credentials.GetToken()
	stream := &costStream{ctx: agentContext(lead.ID, 1, token)}
	if err := r.AgentService.WatchRunCost(&pb.WatchRunCostRequest{RunId: string(run.ID)}, stream); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("an agent read its run's cost: %v", err)
	}
	if err := r.Service.WatchRunCost(&pb.WatchRunCostRequest{}, &costStream{ctx: context.Background()}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("a request with no run: %v", err)
	}
}

// costStream is the server end of a WatchRunCost call, to refuse before anything is sent.
type costStream struct {
	grpc.ServerStreamingServer[pb.WatchRunCostResponse]
	ctx context.Context
}

func (s *costStream) Context() context.Context { return s.ctx }
