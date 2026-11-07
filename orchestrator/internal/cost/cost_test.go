package cost_test

import (
	"fmt"
	"math"
	"math/rand/v2"
	"reflect"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/cost"
)

var start = time.Date(2026, time.November, 1, 9, 0, 0, 0, time.UTC)

// world is a generated run: its agents and the turns they ran, as the contract allows them. The
// generator does not sort, dedupe or keep a turn on a registered agent in any way the ledger might
// lean on: turns come in any order and with any status, a cost can be missing from a turn that has
// tokens, and agents of one type share a model or do not.
type world struct {
	run    core.Run
	agents []core.Agent
	turns  []core.Turn
	now    time.Time
}

var (
	roles    = []core.AgentRole{core.RoleLead, core.RolePlanner, core.RoleWorker, core.RoleJudge, core.RoleCombiner, core.RoleProfiler}
	statuses = []core.TurnStatus{core.TurnCompleted, core.TurnCompleted, core.TurnCompleted, core.TurnInterrupted, core.TurnFailed, core.TurnRunning}
	models   = []core.AgentSpec{
		{Harness: core.ClaudeCode},
		{Harness: core.ClaudeCode, Model: "model-small"},
		{Harness: core.Codex, Model: "model-x"},
	}
)

// dollars draws a cost that sums exactly in any order: a multiple of 1/1024 small enough that a run
// of them stays well inside what a float holds exactly.
func dollars(r *rand.Rand) *float64 {
	c := float64(r.IntN(4096)) / 1024
	return &c
}

func tokens(r *rand.Rand) *uint64 {
	if r.IntN(3) == 0 {
		return nil
	}
	n := uint64(r.IntN(1000))
	return &n
}

// spans are how long a run goes on, from instants to years, so that every rung of the ladder and
// the weeks past it are drawn.
var spans = []time.Duration{0, time.Second, 40 * time.Minute, 5 * time.Hour, 50 * time.Hour, 20 * 24 * time.Hour, 400 * 24 * time.Hour, 5000 * 24 * time.Hour}

func generate(seed uint64) world {
	r := rand.New(rand.NewPCG(seed, seed^0x9e3779b9))
	w := world{run: core.Run{ID: "run-1", CreatedAt: start}}
	span := spans[r.IntN(len(spans))]
	w.now = start.Add(span)
	for i := range r.IntN(9) {
		spec := models[r.IntN(len(models))]
		w.agents = append(w.agents, core.Agent{ID: core.AgentID(fmt.Sprintf("agent-%d", i)), Run: w.run.ID, Role: roles[r.IntN(len(roles))], Spec: spec})
	}
	if len(w.agents) == 0 {
		return w
	}
	ordinals := map[core.AgentID]uint32{}
	for range r.IntN(60) {
		a := w.agents[r.IntN(len(w.agents))]
		ordinals[a.ID]++
		u := core.Usage{InputTokens: tokens(r), CacheReadTokens: tokens(r), CacheCreationTokens: tokens(r), OutputTokens: tokens(r), ReasoningTokens: tokens(r)}
		if r.IntN(3) > 0 {
			u.CostUSD = dollars(r)
		}
		end := start.Add(time.Duration(r.Int64N(int64(span) + 1)))
		w.turns = append(w.turns, core.Turn{
			ID: core.TurnID(fmt.Sprintf("%s-t%d", a.ID, ordinals[a.ID])), Agent: a.ID, Ordinal: ordinals[a.ID],
			Status: statuses[r.IntN(len(statuses))], Usage: u, StartedAt: end.Add(-time.Minute), EndedAt: end,
		})
	}
	return w
}

func (w world) agent(id core.AgentID) core.Agent {
	for _, a := range w.agents {
		if a.ID == id {
			return a
		}
	}
	panic("no agent " + string(id))
}

// ledger counts the turns in the given order, and tells it of the agents first or last by the
// flag, since a stream may bring either one first.
func (w world) ledger(order []int, agentsFirst bool) *cost.Ledger {
	var l cost.Ledger
	if agentsFirst {
		for _, a := range w.agents {
			l.Agent(a)
		}
	}
	for _, i := range order {
		l.Add(w.turns[i], w.agent(w.turns[i].Agent))
	}
	if !agentsFirst {
		for _, a := range w.agents {
			l.Agent(a)
		}
	}
	return &l
}

func inOrder(n int) []int {
	order := make([]int, n)
	for i := range order {
		order[i] = i
	}
	return order
}

const seeds = 400

// forEachWorld runs a property over generated worlds, naming the seed of one that fails.
func forEachWorld(t *testing.T, property func(t *testing.T, w world, v cost.View)) {
	t.Helper()
	for seed := range uint64(seeds) {
		w := generate(seed)
		v := w.ledger(inOrder(len(w.turns)), seed%2 == 0).View(w.run, w.now)
		property(t, w, v)
		if t.Failed() {
			t.Fatalf("failed for seed %d", seed)
		}
	}
}

func finished(w world) []core.Turn {
	var out []core.Turn
	for _, t := range w.turns {
		if t.Status != core.TurnRunning {
			out = append(out, t)
		}
	}
	return out
}

func costOf(p *float64) float64 {
	if p == nil {
		return 0
	}
	return *p
}

func tokenKinds(u core.Usage) [5]uint64 {
	var out [5]uint64
	for i, p := range []*uint64{u.InputTokens, u.CacheReadTokens, u.CacheCreationTokens, u.OutputTokens, u.ReasoningTokens} {
		if p != nil {
			out[i] = *p
		}
	}
	return out
}

func sumKinds(a, b [5]uint64) [5]uint64 {
	for i := range a {
		a[i] += b[i]
	}
	return a
}

// The total is the sum of the finished turns' own costs, and its tokens by kind are theirs.
func TestTotalIsWhatTheFinishedTurnsReported(t *testing.T) {
	forEachWorld(t, func(t *testing.T, w world, v cost.View) {
		var want float64
		var kinds [5]uint64
		priced := false
		for _, turn := range finished(w) {
			want += costOf(turn.Usage.CostUSD)
			priced = priced || turn.Usage.CostUSD != nil
			kinds = sumKinds(kinds, tokenKinds(turn.Usage))
		}
		if (v.Total.CostUSD != nil) != priced || costOf(v.Total.CostUSD) != want {
			t.Errorf("total cost %v, want %v (any priced turn: %v)", v.Total.CostUSD, want, priced)
		}
		if got := tokenKinds(v.Total); got != kinds {
			t.Errorf("total tokens %v, want %v", got, kinds)
		}
	})
}

// A running turn is not counted, as the run's own usage leaves it out.
func TestARunningTurnIsNotCounted(t *testing.T) {
	forEachWorld(t, func(t *testing.T, w world, v cost.View) {
		if want := uint32(len(finished(w))); v.Turns != want {
			t.Errorf("%d turns counted, want the %d finished", v.Turns, want)
		}
	})
}

// A turn with tokens and no cost is unpriced, and the number says so. It adds its tokens and never a
// dollar, and a view with nothing priced has no cost at all, never a zero.
func TestUnpricedTurnsAreCountedAndNeverPriced(t *testing.T) {
	forEachWorld(t, func(t *testing.T, w world, v cost.View) {
		var want uint32
		for _, turn := range finished(w) {
			if turn.Usage.CostUSD == nil && tokenKinds(turn.Usage) != [5]uint64{} {
				want++
			}
		}
		if v.Unpriced != want {
			t.Errorf("%d unpriced turns, want %d", v.Unpriced, want)
		}
	})
	forEachWorld(t, func(t *testing.T, w world, _ cost.View) {
		for i := range w.turns {
			w.turns[i].Usage.CostUSD = nil
		}
		v := w.ledger(inOrder(len(w.turns)), true).View(w.run, w.now)
		if v.Total.CostUSD != nil || v.LastHour != nil {
			t.Errorf("a run with no price shows a cost: total %v, last hour %v", v.Total.CostUSD, v.LastHour)
		}
		for _, r := range v.Roles {
			if r.Usage.CostUSD != nil {
				t.Errorf("type %s has a cost %v with no turn priced", r.Role, *r.Usage.CostUSD)
			}
		}
		for _, m := range v.Models {
			if m.Usage.CostUSD != nil {
				t.Errorf("model %v has a cost %v with no turn priced", m.Model, *m.Usage.CostUSD)
			}
		}
		for _, b := range v.Series.Buckets {
			if b.Cost != 0 || len(b.ByModel) != 0 {
				t.Errorf("bucket %v has cost %v with no turn priced", b.Start, b.Cost)
			}
		}
	})
}

// The parts add up to the whole: the types, the models and the buckets each hold exactly the total's
// cost, and the types and models its tokens, turns and unpriced turns.
func TestTotalEqualsTheSumOfParts(t *testing.T) {
	forEachWorld(t, func(t *testing.T, w world, v cost.View) {
		var roleCost, modelCost, bucketCost float64
		var roleKinds, modelKinds [5]uint64
		var roleTurns, modelTurns, roleUnpriced, modelUnpriced uint32
		for _, r := range v.Roles {
			roleCost += costOf(r.Usage.CostUSD)
			roleKinds = sumKinds(roleKinds, tokenKinds(r.Usage))
			roleTurns, roleUnpriced = roleTurns+r.Turns, roleUnpriced+r.Unpriced
		}
		for _, m := range v.Models {
			modelCost += costOf(m.Usage.CostUSD)
			modelKinds = sumKinds(modelKinds, tokenKinds(m.Usage))
			modelTurns, modelUnpriced = modelTurns+m.Turns, modelUnpriced+m.Unpriced
		}
		for _, b := range v.Series.Buckets {
			bucketCost += b.Cost
		}
		total := costOf(v.Total.CostUSD)
		if roleCost != total || modelCost != total || bucketCost != total {
			t.Errorf("total %v, types %v, models %v, buckets %v", total, roleCost, modelCost, bucketCost)
		}
		if kinds := tokenKinds(v.Total); roleKinds != kinds || modelKinds != kinds {
			t.Errorf("total tokens %v, types %v, models %v", kinds, roleKinds, modelKinds)
		}
		if roleTurns != v.Turns || modelTurns != v.Turns || roleUnpriced != v.Unpriced || modelUnpriced != v.Unpriced {
			t.Errorf("turns %d (%d unpriced); types %d (%d), models %d (%d)", v.Turns, v.Unpriced, roleTurns, roleUnpriced, modelTurns, modelUnpriced)
		}
	})
}

// A type counts each of its agents once, however many turns they ran and whether they ran any.
func TestRoleCountsMatchTheAgents(t *testing.T) {
	forEachWorld(t, func(t *testing.T, w world, v cost.View) {
		want := map[core.AgentRole]uint32{}
		for _, a := range w.agents {
			want[a.Role]++
		}
		got := map[core.AgentRole]uint32{}
		for _, r := range v.Roles {
			if _, twice := got[r.Role]; twice {
				t.Errorf("type %s is listed twice", r.Role)
			}
			got[r.Role] = r.Agents
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("agents by type %v, want %v", got, want)
		}
	})
}

// The buckets cut the run's time into consecutive spans of one width from its start to the instant of
// the view, and put each turn in exactly one: the buckets hold the total, and each model's spend in
// them is that model's cost.
func TestBucketsPartitionTheTurns(t *testing.T) {
	forEachWorld(t, func(t *testing.T, w world, v cost.View) {
		s := v.Series
		if !s.Start.Equal(w.run.CreatedAt) || s.Width <= 0 || s.Width%time.Minute != 0 {
			t.Fatalf("series starts %v with width %v", s.Start, s.Width)
		}
		if n := len(s.Buckets); n < 1 || n > cost.MaxBuckets {
			t.Fatalf("%d buckets", n)
		}
		perModel := map[cost.ModelKey]float64{}
		for i, b := range s.Buckets {
			if want := s.Start.Add(time.Duration(i) * s.Width); !b.Start.Equal(want) {
				t.Errorf("bucket %d starts %v, want %v", i, b.Start, want)
			}
			var sum float64
			for j, m := range b.ByModel {
				sum += m.Cost
				perModel[m.Model] += m.Cost
				if m.Cost <= 0 {
					t.Errorf("bucket %d lists %v with cost %v", i, m.Model, m.Cost)
				}
				if j > 0 && indexOfModel(v, b.ByModel[j-1].Model) >= indexOfModel(v, m.Model) {
					t.Errorf("bucket %d is not in the order of the models", i)
				}
			}
			if sum != b.Cost {
				t.Errorf("bucket %d costs %v, its models %v", i, b.Cost, sum)
			}
		}
		last := s.Buckets[len(s.Buckets)-1]
		if !last.Start.Before(v.AsOf) && len(s.Buckets) > 1 {
			t.Errorf("the last bucket starts %v, at or after the view's %v", last.Start, v.AsOf)
		}
		if end := s.Start.Add(time.Duration(len(s.Buckets)) * s.Width); end.Before(v.AsOf) {
			t.Errorf("the buckets end %v, before the view's %v", end, v.AsOf)
		}
		for _, m := range v.Models {
			if perModel[m.Model] != costOf(m.Usage.CostUSD) {
				t.Errorf("model %v: buckets hold %v, its cost is %v", m.Model, perModel[m.Model], costOf(m.Usage.CostUSD))
			}
		}
	})
}

func indexOfModel(v cost.View, k cost.ModelKey) int {
	for i, m := range v.Models {
		if m.Model == k {
			return i
		}
	}
	return -1
}

// Where the buckets fall depends on the turns' ends and nothing else: a turn lands in the bucket
// that holds its end.
func TestATurnIsInTheBucketItEndedIn(t *testing.T) {
	forEachWorld(t, func(t *testing.T, w world, v cost.View) {
		s := v.Series
		want := make([]float64, len(s.Buckets))
		for _, turn := range finished(w) {
			if turn.Usage.CostUSD == nil {
				continue
			}
			i := min(max(int(turn.EndedAt.Sub(s.Start)/s.Width), 0), len(want)-1)
			want[i] += *turn.Usage.CostUSD
		}
		for i, b := range s.Buckets {
			if b.Cost != want[i] {
				t.Errorf("bucket %d holds %v, want %v", i, b.Cost, want[i])
			}
		}
	})
}

// The view is of the turns, not of how they arrived: any order, a turn told twice, and the agents
// told before or after their turns all give the same view.
func TestViewDoesNotDependOnArrival(t *testing.T) {
	for seed := range uint64(seeds) {
		w := generate(seed)
		want := w.ledger(inOrder(len(w.turns)), true).View(w.run, w.now)
		r := rand.New(rand.NewPCG(seed, 1))
		order := inOrder(len(w.turns))
		r.Shuffle(len(order), func(i, j int) { order[i], order[j] = order[j], order[i] })
		order = append(order, order[:len(order)/2]...)
		if got := w.ledger(order, seed%2 == 1).View(w.run, w.now); !reflect.DeepEqual(got, want) {
			t.Fatalf("seed %d: shuffled arrival gives a different view\n got %+v\nwant %+v", seed, got, want)
		}
	}
}

// A view can be asked for again and again, between turns, and each says what the ledger holds then.
func TestAskingAgainChangesNothing(t *testing.T) {
	for seed := range uint64(seeds) {
		w := generate(seed)
		var l cost.Ledger
		for _, a := range w.agents {
			l.Agent(a)
		}
		for i, turn := range w.turns {
			l.Add(turn, w.agent(turn.Agent))
			once := l.View(w.run, w.now)
			if twice := l.View(w.run, w.now); !reflect.DeepEqual(once, twice) {
				t.Fatalf("seed %d after %d turns: a second view differs", seed, i+1)
			}
			if i == len(w.turns)-1 {
				full := w.ledger(inOrder(len(w.turns)), true).View(w.run, w.now)
				if !reflect.DeepEqual(once, full) {
					t.Fatalf("seed %d: asking between turns changed the final view", seed)
				}
			}
		}
	}
}

// The rate counts the priced turns that ended in the hour up to the view's instant, and is absent
// when the run has no priced turn.
func TestLastHourIsTheHourBeforeTheView(t *testing.T) {
	forEachWorld(t, func(t *testing.T, w world, v cost.View) {
		from := v.AsOf.Add(-time.Hour)
		var want float64
		for _, turn := range finished(w) {
			if turn.EndedAt.After(from) && !turn.EndedAt.After(v.AsOf) {
				want += costOf(turn.Usage.CostUSD)
			}
		}
		if (v.LastHour != nil) != (v.Total.CostUSD != nil) {
			t.Fatalf("last hour %v with total %v", v.LastHour, v.Total.CostUSD)
		}
		if v.LastHour != nil && *v.LastHour != want {
			t.Errorf("last hour %v, want %v", *v.LastHour, want)
		}
	})
}

// The hour the rate looks at is open at its start and closed at the view's instant: a turn that
// ended exactly an hour before is out, and one a nanosecond later or exactly at the instant is in.
func TestLastHourBoundaries(t *testing.T) {
	agent := core.Agent{ID: "a", Role: core.RoleWorker, Spec: core.AgentSpec{Harness: core.ClaudeCode}}
	asOf := start.Add(5 * time.Hour)
	ends := map[string]struct {
		end  time.Time
		want float64
	}{
		"an hour before":               {asOf.Add(-time.Hour), 0},
		"a nanosecond inside the hour": {asOf.Add(-time.Hour + 1), 1},
		"at the instant":               {asOf, 1},
	}
	for name, c := range ends {
		var l cost.Ledger
		one := 1.0
		l.Add(core.Turn{Agent: "a", Ordinal: 1, Status: core.TurnCompleted, Usage: core.Usage{CostUSD: &one}, EndedAt: c.end}, agent)
		v := l.View(core.Run{ID: "r", CreatedAt: start}, asOf)
		if v.LastHour == nil || *v.LastHour != c.want {
			t.Errorf("%s: last hour %v, want %v", name, v.LastHour, c.want)
		}
	}
}

// The instant of a view is the given time while the run lives and its end once it is over, so the
// view of a finished run is the same whenever it is asked for. It never falls before the run's start
// or the end of a turn it counts.
func TestAnEndedRunsViewIsFrozen(t *testing.T) {
	for seed := range uint64(seeds) {
		w := generate(seed)
		l := w.ledger(inOrder(len(w.turns)), true)
		live := l.View(w.run, w.now)
		if !live.AsOf.Equal(w.now) {
			t.Fatalf("seed %d: a live run's view is as of %v, want the time given, %v", seed, live.AsOf, w.now)
		}
		ended := w.run
		ended.Status, ended.EndedAt = core.RunEnded, w.now
		first := l.View(ended, w.now)
		later := l.View(ended, w.now.Add(30*24*time.Hour))
		if !reflect.DeepEqual(first, later) {
			t.Fatalf("seed %d: an ended run's view changed with the time it was asked at", seed)
		}
		for _, turn := range finished(w) {
			if turn.EndedAt.After(first.AsOf) {
				t.Fatalf("seed %d: view as of %v leaves out a turn that ended %v", seed, first.AsOf, turn.EndedAt)
			}
		}
		early := l.View(w.run, start.Add(-time.Hour))
		if early.AsOf.Before(start) {
			t.Fatalf("seed %d: a view as of %v is before the run's start", seed, early.AsOf)
		}
	}
}

// The models and types come most costly first, a group with no price after them, so the screen shows
// them in the order it is given.
func TestGroupsAreMostCostlyFirst(t *testing.T) {
	forEachWorld(t, func(t *testing.T, _ world, v cost.View) {
		for i := 1; i < len(v.Roles); i++ {
			if a, b := v.Roles[i-1].Usage.CostUSD, v.Roles[i].Usage.CostUSD; b != nil && (a == nil || *a < *b) {
				t.Errorf("types %s then %s are out of order", v.Roles[i-1].Role, v.Roles[i].Role)
			}
		}
		for i := 1; i < len(v.Models); i++ {
			if a, b := v.Models[i-1].Usage.CostUSD, v.Models[i].Usage.CostUSD; b != nil && (a == nil || *a < *b) {
				t.Errorf("models %v then %v are out of order", v.Models[i-1].Model, v.Models[i].Model)
			}
		}
	})
}

// However long a run has gone on, its series stays within MaxBuckets and the width never shrinks as
// the run grows, so a bucket's edges move only to a wider rung.
func TestWidthKeepsTheSeriesShortAndGrowsWithTheRun(t *testing.T) {
	var last time.Duration
	for span := time.Duration(0); span < 100*365*24*time.Hour; span += span/7 + time.Minute {
		w := cost.WidthFor(span)
		if w < last || w%time.Minute != 0 {
			t.Fatalf("width %v at span %v after %v", w, span, last)
		}
		if n := (span + w - 1) / w; n > cost.MaxBuckets {
			t.Fatalf("a run of %v in %v buckets is %d, over %d", span, w, n, cost.MaxBuckets)
		}
		last = w
	}
}

// Agents with no turn are counted, and a ledger that has seen nothing has one empty bucket.
func TestAnEmptyLedger(t *testing.T) {
	var l cost.Ledger
	run := core.Run{ID: "r", CreatedAt: start, MaxCostUSD: 5}
	v := l.View(run, start)
	if v.Turns != 0 || v.Total.CostUSD != nil || len(v.Roles) != 0 || len(v.Series.Buckets) != 1 || v.LastHour != nil || v.MaxCostUSD != 5 {
		t.Fatalf("empty view %+v", v)
	}
	l.Agent(core.Agent{ID: "a", Role: core.RoleLead, Spec: core.AgentSpec{Harness: core.ClaudeCode}})
	v = l.View(run, start.Add(time.Minute))
	if len(v.Roles) != 1 || v.Roles[0].Agents != 1 || v.Roles[0].Turns != 0 || v.Roles[0].Usage.CostUSD != nil {
		t.Fatalf("an agent with no turn: %+v", v.Roles)
	}
}

// Every type and model says how many tokens it used, in all: the sum of its five kinds. The types'
// totals, the models' totals and the run's own kinds all add up to the same number, so a client never
// sums.
func TestEachGroupServesItsTokenTotal(t *testing.T) {
	sum := func(kinds [5]uint64) (n uint64) {
		for _, k := range kinds {
			n += k
		}
		return n
	}
	forEachWorld(t, func(t *testing.T, w world, v cost.View) {
		var byRole, byModel uint64
		for _, r := range v.Roles {
			if r.Tokens != sum(tokenKinds(r.Usage)) {
				t.Errorf("type %v: %d tokens, its kinds add up to %d", r.Role, r.Tokens, sum(tokenKinds(r.Usage)))
			}
			byRole += r.Tokens
		}
		for _, m := range v.Models {
			if m.Tokens != sum(tokenKinds(m.Usage)) {
				t.Errorf("model %v: %d tokens, its kinds add up to %d", m.Model, m.Tokens, sum(tokenKinds(m.Usage)))
			}
			byModel += m.Tokens
		}
		if total := sum(tokenKinds(v.Total)); byRole != total || byModel != total {
			t.Errorf("run %d tokens, types %d, models %d", total, byRole, byModel)
		}
	})
}

// Counts that would overflow stop at the largest count instead of wrapping to a small number.
func TestTotalTokensStopsAtTheLargestCount(t *testing.T) {
	big := uint64(math.MaxUint64 - 1)
	if got := cost.TotalTokens(core.Usage{InputTokens: &big, OutputTokens: &big}); got != math.MaxUint64 {
		t.Errorf("got %d, want the largest count", got)
	}
	if got := cost.TotalTokens(core.Usage{}); got != 0 {
		t.Errorf("a usage with no tokens totals %d", got)
	}
}
