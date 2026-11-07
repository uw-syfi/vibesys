package daemon_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/cost"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// hostWorld is a host with several runs, each with a lead that answers every turn with the usage a
// test chose, and a watcher of what the host's runs cost.
type hostWorld struct {
	t       *testing.T
	r       *daemontest.Rig
	runs    []core.RunID
	leads   map[core.RunID]core.AgentID
	repos   int
	next    func(agent core.AgentID) *pb.Usage
	watch   *daemon.HostCostWatch
	current *pb.HostCost
}

func newHostWorld(t *testing.T, limits hub.Limits) *hostWorld {
	t.Helper()
	r := daemontest.NewRig(t)
	if limits.MaxItems > 0 {
		r.Limits = limits
		r.Restart(t)
	}
	w := &hostWorld{t: t, r: r, leads: map[core.RunID]core.AgentID{}}
	r.Sidecars.React = func(s *daemontest.Session, cmd *pb.SessionCommand) {
		turn, ok := cmd.GetBody().(*pb.SessionCommand_StartTurn)
		if !ok {
			daemontest.DefaultReaction(s, cmd)
			return
		}
		daemontest.AnswerTurnWith(s, turn.StartTurn.GetTurnId(), "ok", w.next(s.Launch.Session.Agent))
	}
	w.next = func(core.AgentID) *pb.Usage { return nil }
	return w
}

// start begins a run in a repository of its own, with a cost limit when limit is above zero. Its lead
// runs a first turn at once, unless opens is false and the lead's session never opens.
func (w *hostWorld) start(limit float64, opens bool) core.RunID {
	w.t.Helper()
	w.repos++
	spec := &pb.StartRun{
		Repository: fmt.Sprint("/repo-", w.repos), Task: fmt.Sprint("task ", w.repos), Brief: "go", FanoutLimit: 1,
		Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
	}
	if limit > 0 {
		spec.MaxCostUsd = &limit
	}
	if !opens {
		w.r.Sidecars.Opening = func(core.LaunchSession) []*pb.SessionEvent { return nil }
		defer func() { w.r.Sidecars.Opening = nil }()
	}
	resp, err := w.r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: fmt.Sprint("start-", w.repos), Body: &pb.Command_StartRun{StartRun: spec}}})
	if err != nil || resp.GetReceipt().GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		w.t.Fatalf("start: %v, %v", resp, err)
	}
	w.r.Settle(w.t)
	run := core.RunID(resp.GetReceipt().GetRunId())
	state, _ := w.r.Daemon.State().Run(run)
	w.runs = append(w.runs, run)
	w.leads[run] = state.Lead
	return run
}

func (w *hostWorld) command(id string, body *pb.Command) {
	w.t.Helper()
	body.Id = id
	if _, err := w.r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: body}); err != nil {
		w.t.Fatal(err)
	}
	w.r.Settle(w.t)
}

// ask puts a message to a run's lead, which starts a turn that ends with the usage next gives.
func (w *hostWorld) ask(run core.RunID, id string) {
	w.t.Helper()
	w.command(id, &pb.Command{Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(w.leads[run]), Text: "again"}}})
}

func (w *hostWorld) stop(run core.RunID) {
	w.t.Helper()
	w.command(fmt.Sprint("stop-", run), &pb.Command{Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(run)}}})
}

func (w *hostWorld) open(since time.Time) []*pb.WatchHostCostResponse {
	w.t.Helper()
	var first []*pb.WatchHostCostResponse
	var err error
	w.watch, first, err = w.r.Service.WatchHostCostFrom(since)
	if err != nil {
		w.t.Fatal(err)
	}
	w.t.Cleanup(w.watch.Close)
	w.current = first[len(first)-1].GetCost()
	return first
}

// poll reads what the stream has ready and keeps the last view, as a client does.
func (w *hostWorld) poll() []*pb.WatchHostCostResponse {
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

// fresh is the view a client that connects now is given.
func (w *hostWorld) fresh(since time.Time) *pb.HostCost {
	w.t.Helper()
	again, first, err := w.r.Service.WatchHostCostFrom(since)
	if err != nil {
		w.t.Fatal(err)
	}
	defer again.Close()
	return first[0].GetCost()
}

// runCost is the view WatchRunCost gives for a run now.
func (w *hostWorld) runCost(run core.RunID) *pb.RunCost {
	w.t.Helper()
	again, first, err := w.r.Service.WatchRunCostFrom(run)
	if err != nil {
		w.t.Fatal(err)
	}
	defer again.Close()
	return first[0].GetCost()
}

// sameButSequence says two views are the same view as of different log positions: the sequence is the
// one thing that moves with events that cost nothing.
func sameButSequence(a, b *pb.HostCost) bool {
	a, b = proto.Clone(a).(*pb.HostCost), proto.Clone(b).(*pb.HostCost)
	a.Sequence, b.Sequence = 0, 0
	return proto.Equal(a, b)
}

// kinds is the five kinds of token a usage reports, in the order the contract lists them; a kind that
// is not reported is absent.
func kinds(u *pb.Usage) [5]*uint64 {
	return [5]*uint64{u.InputTokens, u.CacheReadTokens, u.CacheCreationTokens, u.OutputTokens, u.ReasoningTokens}
}

// value is a count that may be absent, as zero.
func value(n *uint64) uint64 {
	if n == nil {
		return 0
	}
	return *n
}

func total(u *pb.Usage) uint64 {
	var sum uint64
	for _, n := range kinds(u) {
		sum += value(n)
	}
	return sum
}

// The host's total is the sum of what WatchRunCost says each run's total is, for runs of any number and
// any turns: runs that never ran a turn, turns with no usage, tokens and no price, and a price. The
// tokens by kind, the turns and the unpriced turns are sums too, a cost that no run has is absent and
// not zero, and each run's line in the list is its own view's figures.
func TestHostCostIsTheSumOfEveryRunsCost(t *testing.T) {
	for seed := range uint64(40) {
		rnd := rand.New(rand.NewPCG(seed, 5))
		w := newHostWorld(t, hub.Limits{})
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
		var turning []core.RunID
		for range rnd.IntN(6) {
			limit := 0.0
			if rnd.IntN(2) == 0 {
				limit = float64(1 + rnd.IntN(100))
			}
			opens := rnd.IntN(4) > 0
			run := w.start(limit, opens)
			if opens {
				turning = append(turning, run)
			}
		}
		for i := range rnd.IntN(15) {
			if len(turning) > 0 {
				w.ask(turning[rnd.IntN(len(turning))], fmt.Sprint("ask-", i))
			}
			w.r.Clock.Advance(time.Duration(rnd.IntN(150)) * time.Minute)
		}
		host := w.fresh(time.Time{})
		if err := wire.Validate(&pb.WatchHostCostResponse{Cost: host}); err != nil {
			t.Fatalf("seed %d: the contract refuses the view: %v", seed, err)
		}

		var cost float64
		var sums [5]uint64
		var any [5]bool
		var turns, unpriced, agents uint32
		priced := false
		lines := map[string]*pb.HostRunCost{}
		for _, line := range host.Runs {
			lines[line.RunId] = line
		}
		for _, run := range w.runs {
			rv := w.runCost(run)
			cost += rv.GetTotal().GetCostUsd()
			priced = priced || rv.GetTotal().CostUsd != nil
			for i, n := range kinds(rv.GetTotal()) {
				any[i], sums[i] = any[i] || n != nil, sums[i]+value(n)
			}
			turns, unpriced = turns+rv.Turns, unpriced+rv.UnpricedTurns
			for _, role := range rv.Roles {
				agents += role.Agents
			}
			state, _ := w.r.Daemon.State().Run(run)
			line := lines[string(run)]
			if line == nil {
				t.Fatalf("seed %d: run %s has no line in %v", seed, run, host.Runs)
			}
			if !proto.Equal(line.Usage, rv.Total) || line.Turns != rv.Turns || line.UnpricedTurns != rv.UnpricedTurns || line.TotalTokens != total(rv.Total) ||
				line.Name != state.Task || (line.MaxCostUsd == nil) != (rv.MaxCostUsd == nil) || line.GetMaxCostUsd() != rv.GetMaxCostUsd() || line.Status != wire.RunMessage(state).Status {
				t.Fatalf("seed %d: the line of %s is %v, its view says %v and the run %+v", seed, run, line, rv, state)
			}
		}
		if (host.Total.CostUsd != nil) != priced || host.Total.GetCostUsd() != cost {
			t.Fatalf("seed %d: total cost %v, the runs' views total %v (any priced: %v)", seed, host.Total.CostUsd, cost, priced)
		}
		for i, n := range kinds(host.Total) {
			if (n != nil) != any[i] || value(n) != sums[i] {
				t.Fatalf("seed %d: token kind %d is %v, the runs' views sum %d (reported: %v)", seed, i, n, sums[i], any[i])
			}
		}
		var hostAgents uint32
		for _, role := range host.Roles {
			hostAgents += role.Agents
		}
		if host.Turns != turns || host.UnpricedTurns != unpriced || host.RunCount != uint32(len(w.runs)) || hostAgents != agents {
			t.Fatalf("seed %d: %d turns, %d unpriced, %d runs, %d agents; want %d, %d, %d, %d", seed, host.Turns, host.UnpricedTurns, host.RunCount, hostAgents, turns, unpriced, len(w.runs), agents)
		}
	}
}

// The list keeps the most costly runs up to its limit, the most first, and everything it leaves out is
// added up in others, so that the lines and the others are the host's total and nobody is lost.
func TestHostCostListsTheMostCostlyRunsAndSumsTheRest(t *testing.T) {
	w := newHostWorld(t, hub.Limits{})
	price := map[core.AgentID]float64{}
	w.next = func(agent core.AgentID) *pb.Usage {
		if c, ok := price[agent]; ok {
			return dollars(c)
		}
		tokens := uint64(10)
		return &pb.Usage{InputTokens: &tokens}
	}
	const runs = cost.MaxHostRuns + 5
	for i := range runs {
		// Every fifth run reports tokens and no price; each of the others costs a different quarter.
		run := w.start(0, true)
		if i%5 != 0 {
			price[w.leads[run]] = float64(i+1) / 4
		}
		w.ask(run, fmt.Sprint("ask-", i))
	}
	host := w.fresh(time.Time{})
	if err := wire.Validate(&pb.WatchHostCostResponse{Cost: host}); err != nil {
		t.Fatalf("the contract refuses the view: %v", err)
	}
	if len(host.Runs) != cost.MaxHostRuns || host.OtherRuns.GetRuns() != 5 || host.RunCount != runs {
		t.Fatalf("%d lines and %d others of %d runs, want %d and 5", len(host.Runs), host.OtherRuns.GetRuns(), host.RunCount, cost.MaxHostRuns)
	}
	var listed float64
	for i, line := range host.Runs {
		listed += line.Usage.GetCostUsd()
		if i > 0 && line.Usage.GetCostUsd() > host.Runs[i-1].Usage.GetCostUsd() {
			t.Fatalf("line %d costs %v after a line of %v", i, line.Usage.GetCostUsd(), host.Runs[i-1].Usage.GetCostUsd())
		}
		if line.Usage.CostUsd != nil && line.Usage.GetCostUsd() < host.OtherRuns.GetUsage().GetCostUsd()/5 {
			t.Fatalf("a listed run of %v is cheaper than the average of those left out", line.Usage.GetCostUsd())
		}
	}
	if got := listed + host.OtherRuns.GetUsage().GetCostUsd(); got != host.Total.GetCostUsd() {
		t.Fatalf("lines %v and others %v are %v, the total is %v", listed, host.OtherRuns.GetUsage().GetCostUsd(), got, host.Total.GetCostUsd())
	}
	if host.OtherRuns.GetTurns()+sumTurns(host.Runs) != host.Turns {
		t.Fatalf("lines and others hold %d turns, the host %d", host.OtherRuns.GetTurns()+sumTurns(host.Runs), host.Turns)
	}
}

func sumTurns(lines []*pb.HostRunCost) uint32 {
	var n uint32
	for _, l := range lines {
		n += l.Turns
	}
	return n
}

// A turn that ends in any run, a run that begins and a run that ends each send the whole view, and the
// events of a poll make one view and not one for each.
func TestHostCostSendsAViewWhenATurnLandsInAnyRun(t *testing.T) {
	w := newHostWorld(t, hub.Limits{})
	w.next = func(core.AgentID) *pb.Usage { return dollars(1) }
	a, b, c := w.start(0, true), w.start(0, true), w.start(0, true)
	if first := w.open(time.Time{}); len(first) != 1 {
		t.Fatalf("%d responses at the start, want the one view", len(first))
	}
	if got := w.poll(); len(got) != 0 {
		t.Fatalf("a poll with nothing new sent %d frames", len(got))
	}
	before := w.current.Total.GetCostUsd()
	if w.current.RunCount != 3 || before != 3 {
		t.Fatalf("the first view counts %d runs and %v dollars, want three runs with a turn each", w.current.RunCount, before)
	}
	for i, run := range []core.RunID{c, a, b} {
		w.ask(run, fmt.Sprint("ask-", i))
		got := w.poll()
		if len(got) != 1 {
			t.Fatalf("%d frames when a turn ended in run %s, want one", len(got), run)
		}
		before++
		if cost := w.current.Total.GetCostUsd(); cost != before {
			t.Fatalf("after a turn in %s the view totals %v, want %v", run, cost, before)
		}
	}
	for i, run := range []core.RunID{a, b, c, a} {
		w.ask(run, fmt.Sprint("again-", i))
	}
	if got := w.poll(); len(got) != 1 || w.current.Total.GetCostUsd() != before+4 {
		t.Fatalf("%d frames for the events of four turns in three runs, want one with %v dollars: %v", len(got), before+4, w.current.Total)
	}

	// A run that begins after the stream opened is counted at once, before its lead's first turn ends.
	w.next = func(core.AgentID) *pb.Usage { return dollars(0.5) }
	d := w.start(0, false)
	if got := w.poll(); len(got) != 1 || w.current.RunCount != 4 {
		t.Fatalf("%d frames for a new run, run_count %d", len(got), w.current.RunCount)
	}
	for _, line := range w.current.Runs {
		if line.RunId == string(d) && (line.Turns != 0 || line.Usage.CostUsd != nil) {
			t.Fatalf("the new run has a line of %v, want none of a cost yet", line)
		}
	}

	// A run that ends changes its line.
	w.stop(a)
	w.poll()
	for _, line := range w.current.Runs {
		if line.RunId == string(a) && line.Status != pb.RunStatus_RUN_STATUS_ENDED {
			t.Fatalf("the run that was stopped is %v in the view", line.Status)
		}
	}
	if !sameButSequence(w.current, w.fresh(time.Time{})) {
		t.Fatalf("the view a client keeps differs from a new client's:\n%v\n%v", w.current, w.fresh(time.Time{}))
	}
}

// A new agent counts as one of its type as soon as it exists, before any of its turns ends.
func TestHostCostCountsANewAgentBeforeItsFirstTurn(t *testing.T) {
	w := newHostWorld(t, hub.Limits{})
	w.next = func(core.AgentID) *pb.Usage { return dollars(1) }
	run := w.start(0, true)
	w.open(time.Time{})
	w.r.Sidecars.Opening = func(core.LaunchSession) []*pb.SessionEvent { return nil } // the worker's session never opens.
	token := w.r.Sidecars.Session(core.Session{Agent: w.leads[run], N: 1}).Credentials.GetToken()
	resp, err := w.r.AgentService.Dispatch(agentContext(w.leads[run], 1, token), &pb.DispatchRequest{Command: &pb.Command{Id: "spawn-1", Body: &pb.Command_SpawnAgent{SpawnAgent: &pb.SpawnAgent{
		RunId: string(run), Role: pb.AgentRole_AGENT_ROLE_WORKER, Brief: "work",
	}}}})
	if err != nil || resp.GetReceipt().GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("spawn: %v, %v", resp, err)
	}
	w.r.Settle(t)
	w.poll()
	var workers uint32
	for _, r := range w.current.Roles {
		if r.Role == pb.AgentRole_AGENT_ROLE_WORKER {
			workers = r.Agents
		}
	}
	if workers != 1 {
		t.Fatalf("the view holds %d workers, want the one that was spawned: %v", workers, w.current.Roles)
	}
}

// A host with a live run is worked out again as time passes with nothing happening, so a rate does not
// stand still, and once no run is live its last view is fixed and no later time changes it.
func TestHostCostFollowsTheClockWhileARunLivesAndStopsWhenNoneDoes(t *testing.T) {
	w := newHostWorld(t, hub.Limits{})
	w.next = func(core.AgentID) *pb.Usage { return dollars(2) }
	a, b := w.start(0, true), w.start(0, true)
	w.open(time.Time{})
	if got := w.poll(); len(got) != 0 || w.current.GetLastHourUsd() != 4 {
		t.Fatalf("right after the first view: %v, last hour %v", got, w.current.GetLastHourUsd())
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
	w.stop(a)
	w.poll()
	if got := w.poll(); len(got) != 0 {
		t.Fatalf("a poll with a run still live and nothing new sent %v", got)
	}
	w.stop(b)
	w.poll()
	var latest time.Time
	for _, id := range []core.RunID{a, b} {
		if run, _ := w.r.Daemon.State().Run(id); !run.Status.Final() || run.EndedAt.After(latest) {
			latest = run.EndedAt
		}
	}
	if !w.current.AsOf.AsTime().Equal(latest) {
		t.Fatalf("with no run live the view is as of %v, the last run ended %v", w.current.AsOf.AsTime(), latest)
	}
	w.r.Clock.Advance(3 * daemon.CostRefresh)
	if got := w.poll(); len(got) != 0 {
		t.Fatalf("a view of a host with nothing live changed with the clock: %v", got)
	}
}

// Whatever order turns end in, however often the stream is read, whether runs begin and end meanwhile,
// and whether the watcher falls behind, the view a watcher holds is the view a new watcher is given,
// and its total is the sum of the runs' own usage.
func TestHostCostConvergesWhateverTheScript(t *testing.T) {
	for seed := range uint64(40) {
		rnd := rand.New(rand.NewPCG(seed, 8))
		w := newHostWorld(t, hub.Limits{MaxItems: 1 + rnd.IntN(8), MaxBytes: 1 << 20})
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
		w.start(0, true)
		w.open(time.Time{})
		for i := range 30 {
			switch rnd.IntN(10) {
			case 0:
				w.poll()
			case 1:
				w.r.Clock.Advance(time.Duration(rnd.IntN(150)) * time.Minute)
			case 2:
				w.start(float64(rnd.IntN(3)*10), rnd.IntN(3) > 0)
			case 3:
				w.stop(w.runs[rnd.IntN(len(w.runs))])
			default:
				w.ask(w.runs[rnd.IntN(len(w.runs))], fmt.Sprint("ask-", i))
			}
		}
		for len(w.poll()) > 0 {
		}
		fresh := w.fresh(time.Time{})
		if !sameButSequence(w.current, fresh) {
			t.Fatalf("seed %d: the watcher holds\n%v\na new one is given\n%v", seed, w.current, fresh)
		}
		var usage float64
		for _, run := range w.r.Daemon.State().Runs() {
			usage += costOf(run.Usage)
		}
		if got := fresh.GetTotal().GetCostUsd(); got != usage {
			t.Fatalf("seed %d: total %v, the runs' usage is %v", seed, got, usage)
		}
	}
}

// A window counts the turns that ended in it and the runs that did not end before it, and a window
// that begins after the host's clock is refused.
func TestHostCostWindowCountsWhatHappenedInIt(t *testing.T) {
	w := newHostWorld(t, hub.Limits{})
	w.next = func(core.AgentID) *pb.Usage { return dollars(1) }
	begin := w.r.Clock.Now()
	old := w.start(0, true) // its first turn costs 1 at the start.
	w.r.Clock.Advance(2 * time.Hour)
	w.stop(old)
	w.r.Clock.Advance(time.Hour)
	w.next = func(core.AgentID) *pb.Usage { return dollars(2) }
	recent := w.start(0, true) // its first turn costs 2, three hours in.
	w.r.Clock.Advance(time.Hour)

	costs := func(since time.Time) (cost float64, runs map[string]bool) {
		view := w.fresh(since)
		runs = map[string]bool{}
		for _, line := range view.Runs {
			runs[line.RunId] = true
		}
		return view.Total.GetCostUsd(), runs
	}
	if cost, runs := costs(time.Time{}); cost != 3 || len(runs) != 2 {
		t.Fatalf("with no window: %v dollars in %v, want 3 in two runs", cost, runs)
	}
	// A turn that ended at the very instant the window begins is in it.
	if cost, runs := costs(begin); cost != 3 || len(runs) != 2 {
		t.Fatalf("a window that begins when the old run's turn ended: %v dollars in %v, want 3 in two runs", cost, runs)
	}
	// The old run ended inside this window, but its turn was before it.
	if cost, runs := costs(begin.Add(time.Hour)); cost != 2 || !runs[string(old)] || !runs[string(recent)] {
		t.Fatalf("a window after the old run's turn: %v dollars in %v, want 2 in both runs", cost, runs)
	}
	// The old run ended before this window.
	if cost, runs := costs(begin.Add(2*time.Hour + 30*time.Minute)); cost != 2 || runs[string(old)] || !runs[string(recent)] {
		t.Fatalf("a window after the old run ended: %v dollars in %v, want 2 in the recent run only", cost, runs)
	}
	view := w.fresh(begin.Add(time.Hour))
	if !view.Series.Start.AsTime().Equal(begin.Add(time.Hour)) {
		t.Fatalf("the series starts %v, want the window's start", view.Series.Start.AsTime())
	}
	if err := wire.Validate(&pb.WatchHostCostResponse{Cost: view}); err != nil {
		t.Fatal(err)
	}

	// A stream with a window still follows the host: a turn that ends in it is counted.
	w.open(begin.Add(2*time.Hour + 30*time.Minute))
	w.next = func(core.AgentID) *pb.Usage { return dollars(4) }
	w.ask(recent, "ask-1")
	w.poll()
	if got := w.current.Total.GetCostUsd(); got != 6 || w.current.RunCount != 1 {
		t.Fatalf("the windowed stream totals %v over %d runs, want 6 over one", got, w.current.RunCount)
	}
	if !sameButSequence(w.current, w.fresh(begin.Add(2*time.Hour+30*time.Minute))) {
		t.Fatal("the windowed view a client keeps differs from a new client's")
	}

	if _, _, err := w.r.Service.WatchHostCostFrom(w.r.Clock.Now().Add(time.Second)); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("a window that begins after the host's clock: %v", err)
	}
}

// Only the operator reads the host's cost, and a window that is not an instant of the contract is
// refused before anything is sent.
func TestHostCostIsForTheOperatorAndForValidWindows(t *testing.T) {
	w := newHostWorld(t, hub.Limits{})
	run := w.start(0, true)
	token := w.r.Sidecars.Session(core.Session{Agent: w.leads[run], N: 1}).Credentials.GetToken()
	stream := &hostCostStream{ctx: agentContext(w.leads[run], 1, token)}
	if err := w.r.AgentService.WatchHostCost(&pb.WatchHostCostRequest{}, stream); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("an agent read the host's cost: %v", err)
	}
	for _, since := range []*timestamppb.Timestamp{{Seconds: 0}, {Seconds: -5}, {Seconds: 1, Nanos: 2_000_000_000}} {
		if err := w.r.Service.WatchHostCost(&pb.WatchHostCostRequest{Since: since}, &hostCostStream{ctx: context.Background()}); status.Code(err) != codes.InvalidArgument {
			t.Fatalf("a window of %v: %v", since, err)
		}
	}
	future := timestamppb.New(w.r.Clock.Now().Add(time.Hour))
	if err := w.r.Service.WatchHostCost(&pb.WatchHostCostRequest{Since: future}, &hostCostStream{ctx: context.Background()}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("a window after the clock: %v", err)
	}
}

// hostCostStream is the server end of a WatchHostCost call, to refuse before anything is sent.
type hostCostStream struct {
	grpc.ServerStreamingServer[pb.WatchHostCostResponse]
	ctx context.Context
}

func (s *hostCostStream) Context() context.Context { return s.ctx }
