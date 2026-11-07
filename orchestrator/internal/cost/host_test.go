package cost_test

import (
	"cmp"
	"fmt"
	"math/rand/v2"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/cost"
)

// hostWorld is a generated host: its runs, their agents and the turns the agents ran, as the
// contract allows them. As in world, nothing is sorted or deduplicated for the host to lean on: runs
// are live or over with any status, a run may have no agent or no turn, a turn may have tokens and no
// cost, and a host may hold more runs than the host view lists.
type hostWorld struct {
	runs   []core.Run
	agents []core.Agent
	turns  []core.Turn
	now    time.Time
	// byID and views are filled by index, so that a property does not search the agents or work out a
	// run's view again each time it asks.
	byID  map[core.AgentID]core.Agent
	views map[core.RunID]cost.View
}

// index makes the world ready to be asked about.
func (w hostWorld) index() hostWorld {
	w.byID, w.views = map[core.AgentID]core.Agent{}, map[core.RunID]cost.View{}
	for _, a := range w.agents {
		w.byID[a.ID] = a
	}
	return w
}

var runStatuses = []core.RunStatus{core.RunStarting, core.RunRunning, core.RunStopping, core.RunEnded, core.RunFailed}

func generateHost(seed uint64) hostWorld {
	r := rand.New(rand.NewPCG(seed, seed^0x51ed270b))
	span := spans[r.IntN(len(spans))]
	w := hostWorld{now: start.Add(span)}
	n := r.IntN(8)
	if r.IntN(8) == 0 {
		n = cost.MaxHostRuns + r.IntN(20)
	}
	for k := range n {
		created := start.Add(time.Duration(r.Int64N(int64(span) + 1)))
		run := core.Run{
			ID: core.RunID(fmt.Sprintf("run-%02d", k)), Task: fmt.Sprintf("task %d", k), Status: runStatuses[r.IntN(len(runStatuses))],
			CreatedAt: created,
		}
		last := w.now
		if run.Status.Final() {
			run.EndedAt = created.Add(time.Duration(r.Int64N(int64(w.now.Sub(created)) + 1)))
			last = run.EndedAt
		}
		if r.IntN(2) == 0 {
			run.MaxCostUSD = float64(1 + r.IntN(500))
		}
		w.runs = append(w.runs, run)
		w.addAgentsAndTurns(r, run, last)
	}
	return w.index()
}

func (w *hostWorld) addAgentsAndTurns(r *rand.Rand, run core.Run, last time.Time) {
	var agents []core.Agent
	for i := range r.IntN(4) {
		agents = append(agents, core.Agent{
			ID: core.AgentID(fmt.Sprintf("%s-agent-%d", run.ID, i)), Run: run.ID, Role: roles[r.IntN(len(roles))], Spec: models[r.IntN(len(models))],
		})
	}
	w.agents = append(w.agents, agents...)
	if len(agents) == 0 {
		return
	}
	ordinals := map[core.AgentID]uint32{}
	for range r.IntN(16) {
		a := agents[r.IntN(len(agents))]
		ordinals[a.ID]++
		u := core.Usage{InputTokens: tokens(r), CacheReadTokens: tokens(r), CacheCreationTokens: tokens(r), OutputTokens: tokens(r), ReasoningTokens: tokens(r)}
		if r.IntN(3) > 0 {
			u.CostUSD = dollars(r)
		}
		end := run.CreatedAt.Add(time.Duration(r.Int64N(int64(last.Sub(run.CreatedAt)) + 1)))
		w.turns = append(w.turns, core.Turn{
			ID: core.TurnID(fmt.Sprintf("%s-t%d", a.ID, ordinals[a.ID])), Agent: a.ID, Ordinal: ordinals[a.ID],
			Status: statuses[r.IntN(len(statuses))], Usage: u, StartedAt: end.Add(-time.Minute), EndedAt: end,
		})
	}
}

func (w hostWorld) agent(id core.AgentID) core.Agent { return w.byID[id] }

// runView is the view WatchRunCost serves for one run of the world: the oracle every host figure is
// held to.
func (w hostWorld) runView(run core.Run) cost.View {
	if v, ok := w.views[run.ID]; ok {
		return v
	}
	var l cost.Ledger
	for _, a := range w.agents {
		if a.Run == run.ID {
			l.Agent(a)
		}
	}
	for _, t := range w.turns {
		if a := w.agent(t.Agent); a.Run == run.ID {
			l.Add(t, a)
		}
	}
	w.views[run.ID] = l.View(run, w.now)
	return w.views[run.ID]
}

// host tells a host of the world in an order drawn from seed: the runs shuffled, and for each the
// agents before or after the turns, the turns shuffled and some of them told twice. A run's agents and
// turns follow the run, as the host's stream tells them.
func (w hostWorld) host(since time.Time, seed uint64) *cost.Host {
	r := rand.New(rand.NewPCG(seed, 77))
	h := cost.NewHost(since)
	runs := slices.Clone(w.runs)
	r.Shuffle(len(runs), func(i, j int) { runs[i], runs[j] = runs[j], runs[i] })
	agentsFirst := r.IntN(2) == 0
	for _, run := range runs {
		h.Run(run)
		tell := func() {
			for _, a := range w.agents {
				if a.Run == run.ID {
					h.Agent(a)
				}
			}
		}
		if agentsFirst {
			tell()
		}
		var turns []core.Turn
		for _, t := range w.turns {
			if w.agent(t.Agent).Run == run.ID {
				turns = append(turns, t)
			}
		}
		r.Shuffle(len(turns), func(i, j int) { turns[i], turns[j] = turns[j], turns[i] })
		turns = append(turns, turns[:len(turns)/2]...)
		for _, t := range turns {
			h.Add(t, w.agent(t.Agent))
		}
		if !agentsFirst {
			tell()
		}
		h.Run(run)
	}
	return h
}

func forEachHost(t *testing.T, property func(t *testing.T, w hostWorld, v cost.HostView)) {
	t.Helper()
	for seed := range uint64(seeds) {
		w := generateHost(seed)
		property(t, w, w.host(time.Time{}, seed).View(w.now))
		if t.Failed() {
			t.Fatalf("failed for seed %d", seed)
		}
	}
}

// The host's total is the sum of what each run's own view totals, and so are its turns and unpriced
// turns: the tokens by kind exactly, the cost to the dollar, and no cost at all when no run has a
// priced turn.
func TestHostTotalIsTheSumOfEveryRunsTotal(t *testing.T) {
	forEachHost(t, func(t *testing.T, w hostWorld, v cost.HostView) {
		var want float64
		var kinds [5]uint64
		var turns, unpriced uint32
		priced := false
		for _, run := range w.runs {
			rv := w.runView(run)
			want += costOf(rv.Total.CostUSD)
			priced = priced || rv.Total.CostUSD != nil
			kinds = sumKinds(kinds, tokenKinds(rv.Total))
			turns, unpriced = turns+rv.Turns, unpriced+rv.Unpriced
		}
		if (v.Total.CostUSD != nil) != priced || costOf(v.Total.CostUSD) != want {
			t.Errorf("total cost %v, want the runs' %v (any priced: %v)", v.Total.CostUSD, want, priced)
		}
		if got := tokenKinds(v.Total); got != kinds {
			t.Errorf("total tokens %v, want %v", got, kinds)
		}
		if v.Turns != turns || v.Unpriced != unpriced || v.RunCount != uint32(len(w.runs)) {
			t.Errorf("%d turns, %d unpriced, %d runs; want %d, %d, %d", v.Turns, v.Unpriced, v.RunCount, turns, unpriced, len(w.runs))
		}
	})
}

// fullOrder is the order the host lists runs in, stated independently of the host: the most costly
// first, a run with no priced turn after them, and ties by the run's id.
func fullOrder(w hostWorld) []core.RunID {
	type line struct {
		id   core.RunID
		cost *float64
	}
	var lines []line
	for _, run := range w.runs {
		lines = append(lines, line{run.ID, w.runView(run).Total.CostUSD})
	}
	slices.SortFunc(lines, func(a, b line) int {
		switch {
		case a.cost != nil && b.cost != nil:
			return cmp.Or(cmp.Compare(*b.cost, *a.cost), cmp.Compare(a.id, b.id))
		case a.cost != nil:
			return -1
		case b.cost != nil:
			return 1
		}
		return cmp.Compare(a.id, b.id)
	})
	ids := make([]core.RunID, len(lines))
	for i, l := range lines {
		ids[i] = l.id
	}
	return ids
}

// Each line of the list says what its run's own view says, the list is the head of the most-costly-first
// order of every run, and the runs it leaves out are added up in others: a list of at most the limit,
// however many runs the host holds, that loses nobody.
func TestHostListsTheMostCostlyRunsAndSumsTheRest(t *testing.T) {
	forEachHost(t, func(t *testing.T, w hostWorld, v cost.HostView) {
		order := fullOrder(w)
		keep := min(len(order), cost.MaxHostRuns)
		if len(v.Runs) != keep || v.Others.Runs != uint32(len(order)-keep) || v.RunCount != uint32(len(order)) {
			t.Fatalf("%d lines and %d others of %d runs, want %d and %d", len(v.Runs), v.Others.Runs, len(order), keep, len(order)-keep)
		}
		byID := map[core.RunID]core.Run{}
		for _, run := range w.runs {
			byID[run.ID] = run
		}
		for i, line := range v.Runs {
			if line.Run != order[i] {
				t.Fatalf("line %d is %s, want %s", i, line.Run, order[i])
			}
			run, rv := byID[line.Run], w.runView(byID[line.Run])
			if line.Name != run.Task || line.Status != run.Status || line.MaxCostUSD != run.MaxCostUSD {
				t.Errorf("line %s says %q %s %v, the run is %q %s %v", line.Run, line.Name, line.Status, line.MaxCostUSD, run.Task, run.Status, run.MaxCostUSD)
			}
			if !reflect.DeepEqual(line.Usage, rv.Total) || line.Turns != rv.Turns || line.Unpriced != rv.Unpriced || line.Tokens != cost.TotalTokens(rv.Total) {
				t.Errorf("line %s holds %+v, %d turns, %d unpriced; the run's view holds %+v, %d, %d", line.Run, line.Usage, line.Turns, line.Unpriced, rv.Total, rv.Turns, rv.Unpriced)
			}
		}
		var rest cost.OtherRuns
		var restCost float64
		var restKinds [5]uint64
		for _, id := range order[keep:] {
			rv := w.runView(byID[id])
			rest.Turns, rest.Unpriced = rest.Turns+rv.Turns, rest.Unpriced+rv.Unpriced
			restCost += costOf(rv.Total.CostUSD)
			restKinds = sumKinds(restKinds, tokenKinds(rv.Total))
		}
		if v.Others.Turns != rest.Turns || v.Others.Unpriced != rest.Unpriced || costOf(v.Others.Usage.CostUSD) != restCost || tokenKinds(v.Others.Usage) != restKinds {
			t.Errorf("others hold %+v, want %d turns, %d unpriced, %v dollars, tokens %v", v.Others, rest.Turns, rest.Unpriced, restCost, restKinds)
		}
		if v.Others.Tokens != cost.TotalTokens(v.Others.Usage) {
			t.Errorf("others serve %d tokens, their usage adds up to %d", v.Others.Tokens, cost.TotalTokens(v.Others.Usage))
		}
	})
}

// The split by type of agent and by model is the sum over the runs of each run's own split: the same
// groups, with each one's agents, turns, unpriced turns, tokens and cost added up, in the order a
// run's view has them.
func TestHostSplitsAreTheSumOfTheRunsSplits(t *testing.T) {
	forEachHost(t, func(t *testing.T, w hostWorld, v cost.HostView) {
		type sum struct {
			agents, turns, unpriced uint32
			cost                    float64
			kinds                   [5]uint64
		}
		roleSums, modelSums := map[core.AgentRole]*sum{}, map[cost.ModelKey]*sum{}
		add := func(s *sum, agents, turns, unpriced uint32, u core.Usage) {
			s.agents, s.turns, s.unpriced = s.agents+agents, s.turns+turns, s.unpriced+unpriced
			s.cost += costOf(u.CostUSD)
			s.kinds = sumKinds(s.kinds, tokenKinds(u))
		}
		for _, run := range w.runs {
			rv := w.runView(run)
			for _, r := range rv.Roles {
				if roleSums[r.Role] == nil {
					roleSums[r.Role] = &sum{}
				}
				add(roleSums[r.Role], r.Agents, r.Turns, r.Unpriced, r.Usage)
			}
			for _, m := range rv.Models {
				if modelSums[m.Model] == nil {
					modelSums[m.Model] = &sum{}
				}
				add(modelSums[m.Model], 0, m.Turns, m.Unpriced, m.Usage)
			}
		}
		if len(v.Roles) != len(roleSums) || len(v.Models) != len(modelSums) {
			t.Fatalf("%d types and %d models, want %d and %d", len(v.Roles), len(v.Models), len(roleSums), len(modelSums))
		}
		for _, r := range v.Roles {
			want := roleSums[r.Role]
			if want == nil || r.Agents != want.agents || r.Turns != want.turns || r.Unpriced != want.unpriced || costOf(r.Usage.CostUSD) != want.cost || tokenKinds(r.Usage) != want.kinds || r.Tokens != cost.TotalTokens(r.Usage) {
				t.Errorf("type %s holds %+v, want %+v", r.Role, r, want)
			}
		}
		for _, m := range v.Models {
			want := modelSums[m.Model]
			if want == nil || m.Turns != want.turns || m.Unpriced != want.unpriced || costOf(m.Usage.CostUSD) != want.cost || tokenKinds(m.Usage) != want.kinds {
				t.Errorf("model %v holds %+v, want %+v", m.Model, m, want)
			}
		}
		for i := 1; i < len(v.Roles); i++ {
			a, b := v.Roles[i-1], v.Roles[i]
			if a.Usage.CostUSD == nil && b.Usage.CostUSD != nil || a.Usage.CostUSD != nil && b.Usage.CostUSD != nil && *a.Usage.CostUSD < *b.Usage.CostUSD {
				t.Errorf("types %s then %s are not most costly first", a.Role, b.Role)
			}
		}
	})
}

// The series runs from the first counted run's start to the instant of the view, in the buckets the
// ladder gives for that span, and holds every priced turn once: its buckets add up to the total and
// each bucket to its models.
func TestHostSeriesHoldsEveryPricedTurnOnce(t *testing.T) {
	forEachHost(t, func(t *testing.T, w hostWorld, v cost.HostView) {
		s := v.Series
		first := w.now
		for _, run := range w.runs {
			first = earliest(first, run.CreatedAt)
		}
		if len(w.runs) > 0 && !s.Start.Equal(first) {
			t.Errorf("the series starts %v, the first run began %v", s.Start, first)
		}
		if s.Width != cost.WidthFor(v.AsOf.Sub(s.Start)) || len(s.Buckets) < 1 || len(s.Buckets) > cost.MaxBuckets {
			t.Errorf("%d buckets of %v for a span of %v", len(s.Buckets), s.Width, v.AsOf.Sub(s.Start))
		}
		var sum float64
		for i, b := range s.Buckets {
			sum += b.Cost
			if !b.Start.Equal(s.Start.Add(time.Duration(i) * s.Width)) {
				t.Errorf("bucket %d starts %v", i, b.Start)
			}
			var parts float64
			for _, m := range b.ByModel {
				parts += m.Cost
			}
			if parts != b.Cost {
				t.Errorf("bucket %d holds %v, its models %v", i, b.Cost, parts)
			}
		}
		if sum != costOf(v.Total.CostUSD) {
			t.Errorf("the buckets hold %v, the total is %v", sum, costOf(v.Total.CostUSD))
		}
		if end := s.Start.Add(time.Duration(len(s.Buckets)) * s.Width); end.Before(v.AsOf) {
			t.Errorf("the buckets end %v, before the view's %v", end, v.AsOf)
		}
	})
}

func earliest(a, b time.Time) time.Time {
	if b.Before(a) {
		return b
	}
	return a
}

// The rate is the priced turns that ended in the hour up to the view's instant, across every run, and
// absent when none is priced.
func TestHostLastHourIsTheHourBeforeTheView(t *testing.T) {
	forEachHost(t, func(t *testing.T, w hostWorld, v cost.HostView) {
		from := v.AsOf.Add(-time.Hour)
		var want float64
		for _, turn := range w.turns {
			if turn.Status != core.TurnRunning && turn.EndedAt.After(from) && !turn.EndedAt.After(v.AsOf) {
				want += costOf(turn.Usage.CostUSD)
			}
		}
		if (v.LastHour != nil) != (v.Total.CostUSD != nil) || (v.LastHour != nil && *v.LastHour != want) {
			t.Errorf("last hour %v, want %v (total %v)", v.LastHour, want, v.Total.CostUSD)
		}
	})
}

// The view is of the runs and their turns, not of how they arrived: any order of runs, agents and
// turns, a turn told twice, and a run told again give the same view.
func TestHostViewDoesNotDependOnArrival(t *testing.T) {
	for seed := range uint64(seeds) {
		w := generateHost(seed)
		want := w.host(time.Time{}, seed).View(w.now)
		if got := w.host(time.Time{}, seed+1000).View(w.now); !reflect.DeepEqual(got, want) {
			t.Fatalf("seed %d: another arrival gives a different view\n got %+v\nwant %+v", seed, got, want)
		}
	}
}

// With a window the host counts the turns that ended at or after it and the runs that did not end
// before it, and nothing else changes: it is the host of the world cut to that window, with the
// series starting at the window.
func TestHostWindowCountsTheTurnsAndRunsInIt(t *testing.T) {
	for seed := range uint64(seeds) {
		w := generateHost(seed)
		r := rand.New(rand.NewPCG(seed, 9))
		since := start.Add(time.Duration(r.Int64N(int64(w.now.Sub(start)) + 1)))
		got := w.host(since, seed).View(w.now)

		cut := hostWorld{now: w.now}
		counted := map[core.RunID]bool{}
		for _, run := range w.runs {
			if !run.Status.Final() || !run.EndedAt.Before(since) {
				cut.runs = append(cut.runs, run)
				counted[run.ID] = true
			}
		}
		cut.agents = nil
		for _, a := range w.agents {
			if counted[a.Run] {
				cut.agents = append(cut.agents, a)
			}
		}
		for _, turn := range w.turns {
			if counted[w.agent(turn.Agent).Run] && !turn.EndedAt.Before(since) {
				cut.turns = append(cut.turns, turn)
			}
		}
		cut = cut.index()
		want := cut.host(time.Time{}, seed).View(w.now)
		if !got.Series.Start.Equal(since) {
			t.Fatalf("seed %d: the series starts %v, the window %v", seed, got.Series.Start, since)
		}
		got.Series, want.Series = cost.Series{}, cost.Series{}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("seed %d: the window of %v counts\n got %+v\nwant %+v", seed, since, got, want)
		}
	}
}

// The view moves with the clock while a counted run lives and is fixed once none does: its instant is
// the latest end of any counted run or turn, however late it is asked for.
func TestHostViewIsFrozenWhenNothingIsLive(t *testing.T) {
	forEachHost(t, func(t *testing.T, w hostWorld, v cost.HostView) {
		h := w.host(time.Time{}, 3)
		live := h.Live()
		if live != slices.ContainsFunc(w.runs, func(r core.Run) bool { return !r.Status.Final() }) {
			t.Fatalf("Live says %v", live)
		}
		later := h.View(w.now.Add(1000 * time.Hour))
		switch {
		case live || len(w.runs) == 0:
			if !v.AsOf.Equal(w.now) || !later.AsOf.Equal(w.now.Add(1000*time.Hour)) {
				t.Errorf("a host with a live run is as of %v and %v, want the times asked for", v.AsOf, later.AsOf)
			}
		default:
			if !reflect.DeepEqual(v, later) {
				t.Errorf("a host with nothing live changed with the clock:\n%+v\n%+v", v, later)
			}
			if v.AsOf.After(w.now) {
				t.Errorf("as of %v, after the clock %v", v.AsOf, w.now)
			}
		}
		if v.AsOf.Before(v.Series.Start) {
			t.Errorf("as of %v, before the series starts %v", v.AsOf, v.Series.Start)
		}
	})
}

// A host that holds nothing has a view with no cost, one empty bucket and no run, and a run is
// counted from the moment the host is told of it, before any turn.
func TestAnEmptyHostAndARunWithNoTurn(t *testing.T) {
	h := cost.NewHost(time.Time{})
	v := h.View(start)
	if v.Total.CostUSD != nil || v.LastHour != nil || v.RunCount != 0 || len(v.Runs) != 0 || v.Others.Runs != 0 || len(v.Series.Buckets) != 1 || !v.AsOf.Equal(start) {
		t.Fatalf("an empty host: %+v", v)
	}
	run := core.Run{ID: "run-1", Task: "task", Status: core.RunRunning, CreatedAt: start}
	if counted, changed := h.Run(run); !counted || !changed {
		t.Fatalf("a new run: counted %v changed %v", counted, changed)
	}
	h.Agent(core.Agent{ID: "lead", Run: "run-1", Role: core.RoleLead, Spec: core.AgentSpec{Harness: core.ClaudeCode}})
	v = h.View(start.Add(time.Hour))
	if v.RunCount != 1 || len(v.Runs) != 1 || v.Runs[0].Run != "run-1" || v.Runs[0].Usage.CostUSD != nil || v.Total.CostUSD != nil || v.Turns != 0 {
		t.Fatalf("a run with no turn: %+v", v)
	}
	if len(v.Roles) != 1 || v.Roles[0].Agents != 1 {
		t.Fatalf("the run's agent is not counted by type: %+v", v.Roles)
	}
}

// The host says when what it shows of a run changed, and only then: a run that is new, one whose
// status moved, and one whose end moved. Telling it the same again, or what it does not show, changes
// nothing, so a stream sends a view only when it has something to say.
func TestHostSaysWhenARunsLineChanged(t *testing.T) {
	h := cost.NewHost(time.Time{})
	run := core.Run{ID: "run-1", Task: "task", Status: core.RunRunning, CreatedAt: start}
	for _, step := range []struct {
		name    string
		edit    func(r *core.Run)
		changed bool
	}{
		{"new", func(*core.Run) {}, true},
		{"again", func(*core.Run) {}, false},
		{"a report posted", func(r *core.Run) { r.ReportsPosted++ }, false},
		{"stopping", func(r *core.Run) { r.Status = core.RunStopping }, true},
		{"ended", func(r *core.Run) { r.Status, r.EndedAt = core.RunEnded, start.Add(time.Hour) }, true},
		{"ended again", func(*core.Run) {}, false},
	} {
		step.edit(&run)
		if counted, changed := h.Run(run); !counted || changed != step.changed {
			t.Fatalf("%s: counted %v changed %v, want changed %v", step.name, counted, changed, step.changed)
		}
	}
}

// A run that ended before the window is not counted, and neither are its agents and turns, however
// the host is told of them.
func TestHostLeavesOutARunThatEndedBeforeTheWindow(t *testing.T) {
	since := start.Add(time.Hour)
	h := cost.NewHost(since)
	old := core.Run{ID: "old", Status: core.RunEnded, CreatedAt: start, EndedAt: since.Add(-time.Second)}
	lead := core.Agent{ID: "lead", Run: "old", Role: core.RoleLead}
	if counted, changed := h.Run(old); counted || changed {
		t.Fatalf("a run that ended before the window: counted %v changed %v", counted, changed)
	}
	c := 1.0
	h.Agent(lead)
	h.Add(core.Turn{ID: "t", Agent: "lead", Ordinal: 1, Status: core.TurnCompleted, EndedAt: since, Usage: core.Usage{CostUSD: &c}}, lead)
	if v := h.View(since.Add(time.Hour)); v.RunCount != 0 || v.Total.CostUSD != nil || len(v.Roles) != 0 {
		t.Fatalf("what was left out is counted: %+v", v)
	}
	// A run still going is counted, and a turn that ended at the window's start is in it.
	if counted, _ := h.Run(core.Run{ID: "now", Status: core.RunRunning, CreatedAt: start}); !counted {
		t.Fatal("a live run is not counted")
	}
	live := core.Agent{ID: "live", Run: "now", Role: core.RoleLead}
	h.Agent(live)
	h.Add(core.Turn{ID: "a", Agent: "live", Ordinal: 1, Status: core.TurnCompleted, EndedAt: since, Usage: core.Usage{CostUSD: &c}}, live)
	h.Add(core.Turn{ID: "b", Agent: "live", Ordinal: 2, Status: core.TurnCompleted, EndedAt: since.Add(-time.Nanosecond), Usage: core.Usage{CostUSD: &c}}, live)
	if v := h.View(since.Add(time.Hour)); costOf(v.Total.CostUSD) != 1 || v.Turns != 1 {
		t.Fatalf("a window counts the turn that ended at its start and not the one before it: %+v", v)
	}
}
