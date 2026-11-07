package window_test

import (
	"fmt"
	"math/rand/v2"
	"reflect"
	"slices"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/cost"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"github.com/uw-syfi/vibesys/orchestrator/internal/window"
)

var t0 = time.Date(2026, time.November, 1, 9, 0, 0, 0, time.UTC)

// grid is how many whole seconds the generated records and windows are spread over. It is small on
// purpose: a record then lands exactly on a window's edge often, which is where a half-open window is
// easiest to get wrong.
const grid = 40

func at(k int) time.Time { return t0.Add(time.Duration(k) * time.Second) }

const ours core.RunID = "r1"

// world is a generated task and run. The generator draws from the contract and not from what the code
// leans on: records arrive in any order, a result may have no time or no job, a job may belong to
// another run or to nobody, a direction may be unnamed or change in the course of the run, a result may
// observe an objective only a bound makes one (declared by the run, by a person or by another run), ids
// may repeat as a stream repeats them, and a plan may be edited before it was made.
type world struct {
	view     search.View
	agents   []core.Agent
	messages []core.Message
	turns    []turn
}

type turn struct {
	agent core.Agent
	turn  core.Turn
}

func dollars(r *rand.Rand) *float64 {
	c := float64(r.IntN(4096)) / 1024 // sums exactly in any order
	return &c
}

func stamp(r *rand.Rand, nilOneIn int) *timestamppb.Timestamp {
	if r.IntN(nilOneIn) == 0 {
		return nil
	}
	return timestamppb.New(at(r.IntN(grid)))
}

func pick[T any](r *rand.Rand, from ...T) T { return from[r.IntN(len(from))] }

func generate(seed uint64) world {
	r := rand.New(rand.NewPCG(seed, seed^0x9e3779b97f4a7c15))
	var w world
	var facts []*cli.TaskFact
	for i := range r.IntN(10) {
		id := fmt.Sprintf("j%d", i)
		job := &cli.Job{Id: id, Spec: &cli.JobSpec{Settings: &cli.JobSpec_Measure{Measure: &cli.MeasureSettings{}}}, FinishedAt: stamp(r, 3)}
		for _, run := range pick(r, []string{"r1"}, []string{"r2"}, []string{"r1", "r2"}, nil) {
			job.Submitters = append(job.Submitters, &cli.Submitter{RunId: run, At: timestamppb.New(t0)})
		}
		facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Job{Job: job}})
		if r.IntN(5) == 0 {
			continue // a job with no result
		}
		result := resultOf(r, id)
		facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Result{Result: result}})
		if r.IntN(5) == 0 {
			facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Result{Result: proto.Clone(result).(*cli.Result)}}) // a stream repeats it
		}
	}
	for seq := range r.IntN(4) {
		origin := &cli.RecordOrigin{RunId: pick(r, "r1", "", "r2"), At: timestamppb.New(t0)}
		facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Bound{Bound: &cli.ObjectiveBound{
			Seq: uint64(seq + 1), Origin: origin, Objective: pick(r, "mem", "lat"), Value: 5, Unit: "u", Note: "derived",
			Direction: pick(r, cli.Direction_DIRECTION_MAXIMIZE, cli.Direction_DIRECTION_MINIMIZE),
		}}})
	}
	facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Result{Result: resultOf(r, "orphan")}})
	for _, number := range r.Perm(6)[:r.IntN(7)] { // one plan to a number, which a stream may repeat
		p := &cli.Plan{
			Number: uint32(1 + number), CreatedAt: stamp(r, 6), UpdatedAt: stamp(r, 6),
			Origin: &cli.RecordOrigin{RunId: pick(r, "r1", "r2", "", "r3"), At: timestamppb.New(at(r.IntN(grid)))},
		}
		for range r.IntN(4) {
			p.Transitions = append(p.Transitions, &cli.PlanTransition{
				Status: cli.PlanStatus_PLAN_STATUS_OPEN,
				Origin: &cli.RecordOrigin{RunId: pick(r, "r1", "r2", "", "r3"), At: timestamppb.New(at(r.IntN(grid)))},
			})
		}
		facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: p}})
		if r.IntN(4) == 0 {
			facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: proto.Clone(p).(*cli.Plan)}})
		}
	}
	w.view = search.Fold(facts)
	for i := range r.IntN(8) {
		a := core.Agent{
			ID: core.AgentID(fmt.Sprintf("a%d", i)), Run: pick(r, ours, ours, "r2"),
			Role:      pick(r, core.RoleLead, core.RoleWorker, core.RoleJudge, core.RolePlanner),
			StartedAt: at(r.IntN(grid)),
		}
		if r.IntN(2) == 0 {
			a.EndedAt = at(r.IntN(grid))
		}
		w.agents = append(w.agents, a)
		for ordinal := range r.IntN(5) {
			u := core.Usage{CostUSD: dollars(r)}
			if r.IntN(3) == 0 {
				n := uint64(1 + r.IntN(1000))
				u = core.Usage{InputTokens: &n} // tokens and no price: unpriced
			}
			status := pick(r, core.TurnCompleted, core.TurnCompleted, core.TurnFailed, core.TurnRunning)
			w.turns = append(w.turns, turn{a, core.Turn{
				ID: core.TurnID(fmt.Sprintf("%s-t%d", a.ID, ordinal)), Agent: a.ID, Ordinal: uint32(ordinal + 1), Status: status,
				Usage: u, StartedAt: at(0), EndedAt: at(r.IntN(grid)),
			}})
		}
	}
	for i := range r.IntN(10) {
		m := core.Message{ID: core.MessageID(fmt.Sprintf("m%d", i)), Run: pick(r, ours, ours, "r2"), At: at(r.IntN(grid))}
		m.Body = pick[core.MessageBody](r, core.Report{Kind: core.ReportProgress, Sequence: uint32(i + 1)}, core.Report{Kind: core.ReportFinal}, core.Text{Text: "hi"})
		w.messages = append(w.messages, m)
	}
	return w
}

func resultOf(r *rand.Rand, job string) *cli.Result {
	m := &cli.MeasureResult{Value: float64(r.IntN(64)) / 4}
	switch r.IntN(4) {
	case 0: // several objectives: the scored metric and one observed
		m.Metric = &cli.Metric{Name: "tput", Direction: cli.Direction_DIRECTION_MAXIMIZE}
		m.Objectives = &cli.Objectives{Metrics: []*cli.Metric{m.Metric, {Name: "lat", Direction: cli.Direction_DIRECTION_MINIMIZE}}}
		if r.IntN(4) != 0 {
			v := float64(r.IntN(64)) / 4
			m.Observations = []*cli.Observation{{Name: "lat", Value: &v}}
		}
	default:
		m.Metric = &cli.Metric{Name: pick(r, "tput", "lat", "acc"), Direction: pick(r, cli.Direction_DIRECTION_MAXIMIZE, cli.Direction_DIRECTION_MINIMIZE, cli.Direction_DIRECTION_MAXIMIZE, cli.Direction_DIRECTION_UNSPECIFIED)}
		if r.IntN(2) == 0 {
			v := float64(r.IntN(64)) / 4
			m.Observations = []*cli.Observation{{Name: "mem", Value: &v}}
		}
	}
	return &cli.Result{JobId: job, Commit: "c-" + job, RecordedAt: stamp(r, 8), Outcome: &cli.Result_Measure{Measure: m}}
}

// input builds the records of a window, in the order and with the repeats the stream gave them.
func (w world) input(r *rand.Rand, since, until time.Time) window.Input {
	ledger := &cost.Ledger{}
	turns := slices.Clone(w.turns)
	r.Shuffle(len(turns), func(i, j int) { turns[i], turns[j] = turns[j], turns[i] })
	for i, t := range turns {
		ledger.Agent(t.agent)
		ledger.Add(t.turn, t.agent)
		if i%3 == 0 {
			ledger.Add(t.turn, t.agent) // a stream that repeats a turn
		}
	}
	facts := slices.Clone(w.view.Facts)
	r.Shuffle(len(facts), func(i, j int) { facts[i], facts[j] = facts[j], facts[i] })
	agents, messages := slices.Clone(w.agents), slices.Clone(w.messages)
	r.Shuffle(len(agents), func(i, j int) { agents[i], agents[j] = agents[j], agents[i] })
	r.Shuffle(len(messages), func(i, j int) { messages[i], messages[j] = messages[j], messages[i] })
	messages = append(messages, messages[:len(messages)/2]...)
	return window.Input{Run: ours, Since: since, Until: until, View: search.Fold(facts), Ledger: ledger, Agents: agents, Messages: messages}
}

func (w world) facts(r *rand.Rand, a, b int) core.WindowFacts {
	return window.Facts(w.input(r, at(a), at(b)))
}

// three draws a window split in two, with the middle anywhere from the start to the end inclusive.
func three(r *rand.Rand) (a, b, c int) {
	xs := []int{r.IntN(grid + 1), r.IntN(grid + 1), r.IntN(grid + 1)}
	slices.Sort(xs)
	return xs[0], xs[1], xs[2]
}

const seeds = 600

func rng(seed uint64) *rand.Rand { return rand.New(rand.NewPCG(seed, 7)) }

// Over [a, c] the spend, the agents and the measurements are those over [a, b] plus those over [b, c], and
// the sets of plans and reports are the unions. The window is half-open, so a record on b is counted by
// exactly one half.
func TestFactsOverASpanAreTheFactsOverItsHalvesAdded(t *testing.T) {
	for seed := range uint64(seeds) {
		w, r := generate(seed), rng(seed)
		a, b, c := three(r)
		whole, left, right := w.facts(r, a, c), w.facts(r, a, b), w.facts(r, b, c)
		fail := func(what string, got, want any) {
			t.Fatalf("seed %d, %d <= %d <= %d: %s over the whole is %v, the halves add to %v", seed, a, b, c, what, got, want)
		}
		if got, want := whole.Spend, addSpend(left.Spend, right.Spend); !reflect.DeepEqual(got, want) {
			fail("spend", got, want)
		}
		if got, want := whole.AgentsStarted, left.AgentsStarted+right.AgentsStarted; got != want {
			fail("agents started", got, want)
		}
		if got, want := whole.AgentsEnded, left.AgentsEnded+right.AgentsEnded; got != want {
			fail("agents ended", got, want)
		}
		if got, want := whole.JobsMeasured, left.JobsMeasured+right.JobsMeasured; got != want {
			fail("jobs measured", got, want)
		}
		if got, want := whole.PlansCreated, union(left.PlansCreated, right.PlansCreated); !slices.Equal(got, want) {
			fail("plans created", got, want)
		}
		if got, want := touched(whole), union(touched(left), touched(right)); !slices.Equal(got, want) {
			fail("plans created or changed", got, want)
		}
		if got, want := whole.Reports, slices.Concat(left.Reports, right.Reports); !slices.Equal(got, want) {
			fail("reports", got, want)
		}
	}
}

func addSpend(a, b core.Spend) core.Spend {
	out := core.Spend{Turns: a.Turns + b.Turns, Unpriced: a.Unpriced + b.Unpriced, Usage: a.Usage.Plus(b.Usage)}
	roles := map[core.AgentRole]core.RoleSpend{}
	for _, s := range slices.Concat(a.Roles, b.Roles) {
		have := roles[s.Role]
		roles[s.Role] = core.RoleSpend{Role: s.Role, Turns: have.Turns + s.Turns, Unpriced: have.Unpriced + s.Unpriced, Usage: have.Usage.Plus(s.Usage)}
	}
	for _, s := range roles {
		out.Roles = append(out.Roles, s)
	}
	slices.SortFunc(out.Roles, func(x, y core.RoleSpend) int { return compare(string(x.Role), string(y.Role)) })
	return out
}

func compare(a, b string) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	}
	return 0
}

func union(a, b []uint32) []uint32 {
	out := slices.Concat(a, b)
	slices.Sort(out)
	return slices.Compact(out)
}

func touched(f core.WindowFacts) []uint32 { return union(f.PlansCreated, f.PlansChanged) }

// The best value of an objective is chained: a window starts where the one before it ended, and a
// span starts where its first half starts and ends where its last half ends.
func TestObjectivesChainAcrossWindows(t *testing.T) {
	for seed := range uint64(seeds) {
		w, r := generate(seed), rng(seed)
		a, b, c := three(r)
		whole, left, right := w.facts(r, a, c), w.facts(r, a, b), w.facts(r, b, c)
		starts := func(f core.WindowFacts) map[string]*core.Best {
			return ends(f, func(m core.ObjectiveMove) *core.Best { return m.Start })
		}
		finishes := func(f core.WindowFacts) map[string]*core.Best {
			return ends(f, func(m core.ObjectiveMove) *core.Best { return m.End })
		}
		if got, want := starts(whole), starts(left); !reflect.DeepEqual(got, want) {
			t.Fatalf("seed %d: the span starts at %v and its first half at %v", seed, got, want)
		}
		if got, want := finishes(whole), finishes(right); !reflect.DeepEqual(got, want) {
			t.Fatalf("seed %d: the span ends at %v and its last half at %v", seed, got, want)
		}
		if got, want := finishes(left), starts(right); !reflect.DeepEqual(got, want) {
			t.Fatalf("seed %d: the first half ends at %v and the second starts at %v", seed, got, want)
		}
	}
}

// ends reads one end of each objective that has it. A start that is nil is absent, not a value.
func ends(f core.WindowFacts, end func(core.ObjectiveMove) *core.Best) map[string]*core.Best {
	out := map[string]*core.Best{}
	for _, m := range f.Objectives {
		if b := end(m); b != nil {
			out[m.Metric] = b
		}
	}
	return out
}

// A window with nothing in it counts nothing, and every objective ends where it began.
func TestAnEmptyWindowHasNoSpendNoCountsAndNoMovement(t *testing.T) {
	for seed := range uint64(seeds) {
		w, r := generate(seed), rng(seed)
		k := r.IntN(grid + 1)
		f := w.facts(r, k, k)
		if !reflect.DeepEqual(f.Spend, core.Spend{}) || f.AgentsStarted != 0 || f.AgentsEnded != 0 || f.JobsMeasured != 0 ||
			len(f.PlansCreated) != 0 || len(f.PlansChanged) != 0 || len(f.Reports) != 0 {
			t.Fatalf("seed %d: the empty window at %d is not empty: %+v", seed, k, f)
		}
		for _, m := range f.Objectives {
			if !reflect.DeepEqual(m.Start, m.End) {
				t.Fatalf("seed %d: objective %s moved in an empty window: %+v to %+v", seed, m.Metric, m.Start, m.End)
			}
		}
	}
}

// What a task's other runs did, and what no run claimed, is not the run's.
func TestOtherRunsRecordsAreNotCounted(t *testing.T) {
	for seed := range uint64(seeds) {
		w, r := generate(seed), rng(seed)
		a, _, c := three(r)
		want := w.facts(r, a, c)
		var mine world
		kept := map[string]bool{}
		for _, f := range w.view.Facts {
			if j := f.GetJob(); j != nil && slices.ContainsFunc(j.Submitters, func(s *cli.Submitter) bool { return s.RunId == string(ours) }) {
				kept[j.Id] = true
			}
		}
		var facts []*cli.TaskFact
		for _, f := range w.view.Facts {
			if j := f.GetJob(); j != nil && !kept[j.Id] || f.GetResult() != nil && !kept[f.GetResult().JobId] {
				continue
			}
			facts = append(facts, f)
		}
		mine.view = search.Fold(facts)
		for _, ag := range w.agents {
			if ag.Run == ours {
				mine.agents = append(mine.agents, ag)
			}
		}
		for _, m := range w.messages {
			if m.Run == ours {
				mine.messages = append(mine.messages, m)
			}
		}
		for _, tn := range w.turns {
			if tn.agent.Run == ours {
				mine.turns = append(mine.turns, tn)
			}
		}
		if len(mine.messages) == 0 {
			continue
		}
		got := mine.facts(r, a, c)
		// The turns of another run's agents were never the ledger's; the run's own ledger is the one given.
		got.Spend, want.Spend = core.Spend{}, core.Spend{}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("seed %d: dropping other runs' agents and messages changed the facts:\n%+v\n%+v", seed, got, want)
		}
	}
}

// The facts do not depend on the order the records came in, or on a record repeated.
func TestFactsDoNotDependOnTheOrderOrRepeatsOfTheRecords(t *testing.T) {
	for seed := range uint64(seeds) {
		w := generate(seed)
		a, _, c := three(rng(seed))
		first, second := w.facts(rng(seed+1000), a, c), w.facts(rng(seed+2000), a, c)
		if !reflect.DeepEqual(first, second) {
			t.Fatalf("seed %d: two orders of the same records give different facts:\n%+v\n%+v", seed, first, second)
		}
	}
}

// charted is a point of a series the run's state serves, as the report draws it.
type charted struct {
	at          time.Time
	value       float64
	job, commit string
}

// bestIn is where the best of a series stood before t, written from its points alone: the best value
// among those dated before t, the earliest of equal values keeping it. It is nil when none is.
func bestIn(points []charted, direction cli.Direction, t time.Time) *charted {
	var best *charted
	for i, p := range points {
		if p.at.Before(t) && (best == nil || (direction == cli.Direction_DIRECTION_MAXIMIZE && p.value > best.value) || (direction == cli.Direction_DIRECTION_MINIMIZE && p.value < best.value)) {
			best = &points[i]
		}
	}
	return best
}

func sameBest(got *core.Best, want *charted) bool {
	if got == nil || want == nil {
		return got == nil && want == nil
	}
	return got.Value == want.value && string(got.Job) == want.job && string(got.Commit) == want.commit && got.At.Equal(want.at)
}

// What a check-in freezes is what the report shows: at the start and at the end of any window, the best of
// each objective is the best of the series the run's state serves for it, with the candidate that made
// it, and no objective is in one and not the other. They read the same measurements.
func TestACheckInsBestsAreTheReportsBests(t *testing.T) {
	for seed := range uint64(seeds) {
		w, r := generate(seed), rng(seed)
		series, _, _ := search.Trajectory(w.view.Facts, string(ours))
		a, b, _ := three(r)
		moves := map[string]core.ObjectiveMove{}
		for _, m := range window.Facts(w.input(r, at(a), at(b))).Objectives {
			moves[m.Metric] = m
		}
		inReport := map[string]bool{}
		for _, s := range series {
			inReport[s.Objective] = true
			var points []charted
			for _, p := range s.Points {
				points = append(points, charted{at: p.At.AsTime(), value: p.Value, job: p.JobId, commit: p.Commit})
			}
			end, start := bestIn(points, s.Direction, at(b)), bestIn(points, s.Direction, at(a))
			move, listed := moves[s.Objective]
			if end == nil {
				if listed {
					t.Fatalf("seed %d: %s has nothing before %v in the report but a best in the check-in: %+v", seed, s.Objective, at(b), move)
				}
				continue
			}
			wantDirection := core.Maximize
			if s.Direction == cli.Direction_DIRECTION_MINIMIZE {
				wantDirection = core.Minimize
			}
			if !listed || move.Direction != wantDirection || !sameBest(move.End, end) || !sameBest(move.Start, start) {
				t.Fatalf("seed %d: %s over [%d, %d) is %+v in the check-in; the report's points %v say end %v start %v", seed, s.Objective, a, b, move, points, end, start)
			}
		}
		for name := range moves {
			if !inReport[name] {
				t.Fatalf("seed %d: %s is in the check-in and not in the report", seed, name)
			}
		}
	}
}

func job(id string, runs ...string) *cli.TaskFact {
	j := &cli.Job{Id: id}
	for _, run := range runs {
		j.Submitters = append(j.Submitters, &cli.Submitter{RunId: run})
	}
	return &cli.TaskFact{Fact: &cli.TaskFact_Job{Job: j}}
}

func measure(id, metric string, d cli.Direction, value float64, k int) *cli.TaskFact {
	return &cli.TaskFact{Fact: &cli.TaskFact_Result{Result: &cli.Result{
		JobId: id, Commit: "c-" + id, RecordedAt: timestamppb.New(at(k)),
		Outcome: &cli.Result_Measure{Measure: &cli.MeasureResult{Metric: &cli.Metric{Name: metric, Direction: d}, Value: value}},
	}}}
}

func factsOf(t *testing.T, since, until int, tf ...*cli.TaskFact) core.WindowFacts {
	t.Helper()
	return window.Facts(window.Input{Run: ours, Since: at(since), Until: at(until), View: search.Fold(tf)})
}

// The best of an objective follows its direction, and the earliest of two equal values keeps it, with
// the candidate that made it.
func TestBestFollowsTheDirectionAndNamesItsCandidate(t *testing.T) {
	for _, tc := range []struct {
		direction cli.Direction
		wantJob   core.JobID
		wantValue float64
	}{
		{cli.Direction_DIRECTION_MAXIMIZE, "j2", 9},
		{cli.Direction_DIRECTION_MINIMIZE, "j1", 3},
	} {
		f := factsOf(t, 0, 20,
			job("j1", "r1"), job("j2", "r1"), job("j3", "r1"),
			measure("j1", "m", tc.direction, 3, 5), measure("j2", "m", tc.direction, 9, 6), measure("j3", "m", tc.direction, 9, 7))
		if len(f.Objectives) != 1 || f.Objectives[0].Start != nil {
			t.Fatalf("%v: %+v", tc.direction, f.Objectives)
		}
		got := f.Objectives[0].End
		if got.Job != tc.wantJob || got.Value != tc.wantValue || got.Commit != core.Commit("c-"+string(tc.wantJob)) || !got.At.Equal(at(map[core.JobID]int{"j1": 5, "j2": 6}[tc.wantJob])) {
			t.Fatalf("%v: best is %+v", tc.direction, got)
		}
	}
}

// An objective with no named direction has no best, and is left out, rather than guessed at.
func TestAnObjectiveWithNoDirectionIsLeftOut(t *testing.T) {
	f := factsOf(t, 0, 20, job("j1", "r1"), measure("j1", "m", cli.Direction_DIRECTION_UNSPECIFIED, 3, 5))
	if len(f.Objectives) != 0 || f.JobsMeasured != 1 {
		t.Fatalf("%+v", f)
	}
}

// A measurement on the start of a window was recorded in it and is not part of where the window began.
func TestAMeasurementOnTheEdgeBelongsToTheWindowItOpens(t *testing.T) {
	f := factsOf(t, 5, 9, job("j1", "r1"), job("j2", "r1"),
		measure("j1", "m", cli.Direction_DIRECTION_MAXIMIZE, 1, 5), measure("j2", "m", cli.Direction_DIRECTION_MAXIMIZE, 2, 9))
	m := f.Objectives[0]
	if m.Start != nil || m.End == nil || m.End.Job != "j1" || f.JobsMeasured != 1 {
		t.Fatalf("a window [5, 9) with measurements at 5 and 9: %+v, measured %d", m, f.JobsMeasured)
	}
}

// A result with no time of its own is placed by its job's end, and with neither it is not placed at all.
func TestAResultWithNoTimeIsPlacedByItsJobOrLeftOut(t *testing.T) {
	result := func(id string, tf *cli.TaskFact) *cli.TaskFact {
		tf.GetResult().RecordedAt = nil
		return tf
	}
	finished := job("j1", "r1")
	finished.GetJob().FinishedAt = timestamppb.New(at(6))
	f := factsOf(t, 5, 9, finished, job("j2", "r1"),
		result("j1", measure("j1", "m", cli.Direction_DIRECTION_MAXIMIZE, 1, 0)), result("j2", measure("j2", "m", cli.Direction_DIRECTION_MAXIMIZE, 2, 0)))
	if f.JobsMeasured != 1 || f.Objectives[0].End.Job != "j1" {
		t.Fatalf("%+v", f)
	}
}

// With several objectives each has its own value: the scored metric's, and the headline of the
// observation of every other. An observation with no values has none, so that objective has no best.
func TestSeveralObjectivesEachHaveTheirOwnBest(t *testing.T) {
	lat := 4.0
	tput := measure("j1", "tput", cli.Direction_DIRECTION_MAXIMIZE, 10, 5)
	m := tput.GetResult().GetMeasure()
	m.Objectives = &cli.Objectives{Metrics: []*cli.Metric{m.Metric, {Name: "lat", Direction: cli.Direction_DIRECTION_MINIMIZE}, {Name: "acc", Direction: cli.Direction_DIRECTION_MAXIMIZE}}}
	m.Observations = []*cli.Observation{{Name: "lat", Value: &lat}, {Name: "acc"}}
	f := factsOf(t, 0, 9, job("j1", "r1"), tput)
	got := map[string]float64{}
	for _, o := range f.Objectives {
		got[o.Metric] = o.End.Value
	}
	if !reflect.DeepEqual(got, map[string]float64{"tput": 10, "lat": 4}) {
		t.Fatalf("%v", got)
	}
}

// A price no turn reported is absent: turns with tokens and no cost count as unpriced and add no
// dollars, and a window with no turn has no usage at all.
func TestAbsentPricesStayAbsent(t *testing.T) {
	agent := core.Agent{ID: "a", Run: ours, Role: core.RoleWorker}
	n := uint64(5)
	price := 1.5
	l := &cost.Ledger{}
	l.Agent(agent)
	l.Add(core.Turn{ID: "t1", Agent: "a", Ordinal: 1, Status: core.TurnCompleted, EndedAt: at(5), Usage: core.Usage{InputTokens: &n}}, agent)
	l.Add(core.Turn{ID: "t2", Agent: "a", Ordinal: 2, Status: core.TurnCompleted, EndedAt: at(15), Usage: core.Usage{CostUSD: &price}}, agent)
	f := window.Facts(window.Input{Run: ours, Since: at(0), Until: at(10), Ledger: l})
	if f.Spend.Turns != 1 || f.Spend.Unpriced != 1 || f.Spend.Usage.CostUSD != nil || *f.Spend.Usage.InputTokens != 5 {
		t.Fatalf("a window of one unpriced turn: %+v", f.Spend)
	}
	if len(f.Spend.Roles) != 1 || f.Spend.Roles[0].Role != core.RoleWorker || f.Spend.Roles[0].Usage.CostUSD != nil {
		t.Fatalf("by role: %+v", f.Spend.Roles)
	}
	both := window.Facts(window.Input{Run: ours, Since: at(0), Until: at(20), Ledger: l})
	if both.Spend.Usage.CostUSD == nil || *both.Spend.Usage.CostUSD != 1.5 || both.Spend.Unpriced != 1 {
		t.Fatalf("a window of both: %+v", both.Spend)
	}
	none := window.Facts(window.Input{Run: ours, Since: at(30), Until: at(40), Ledger: l})
	if !reflect.DeepEqual(none.Spend, core.Spend{}) {
		t.Fatalf("a window with no turn: %+v", none.Spend)
	}
}

// ownsPlan is the rule that a plan is the run's, spelled out from the contract: the run made it
// (the plan's origin), or the run set one of its statuses (a transition's origin). Nothing else makes
// a plan the run's, neither when it was made or edited nor who else touched it.
func ownsPlan(p *cli.Plan) bool {
	return p.GetOrigin().GetRunId() == string(ours) || slices.ContainsFunc(p.GetTransitions(), func(t *cli.PlanTransition) bool {
		return t.GetOrigin().GetRunId() == string(ours)
	})
}

// The plans a window counts are the run's alone: a world with every plan the run did not make or set
// a status of taken out has the same window facts as the whole world, whatever times the other plans
// and the other runs' and persons' changes carry.
func TestAWindowCountsOnlyThePlansOfTheRun(t *testing.T) {
	for seed := range uint64(seeds) {
		w, r := generate(seed), rng(seed)
		a, _, c := three(r)
		var own []*cli.TaskFact
		for _, f := range w.view.Facts {
			if p := f.GetPlan(); p == nil || ownsPlan(p) {
				own = append(own, f)
			}
		}
		only := w
		only.view = search.Fold(own)
		got, want := w.facts(rng(seed), a, c), only.facts(rng(seed), a, c)
		if !slices.Equal(got.PlansCreated, want.PlansCreated) || !slices.Equal(got.PlansChanged, want.PlansChanged) {
			t.Fatalf("seed %d, %d..%d: plans %v created %v changed, the run's own alone give %v and %v",
				seed, a, c, got.PlansCreated, got.PlansChanged, want.PlansCreated, want.PlansChanged)
		}
	}
}

// A plan the run made is created in the window it is made in, and changed in a later one when the run
// sets a status of it then, never both. A plan another run or a person made is the run's only for the
// statuses the run set, and one nobody of the run touched is no one's: its dates say nothing.
func TestAPlanIsCreatedOnceAndChangedAfter(t *testing.T) {
	type change struct {
		run string
		at  int
	}
	plan := func(n uint32, creator string, created int, changes ...change) *cli.TaskFact {
		p := &cli.Plan{Number: n, CreatedAt: timestamppb.New(at(created)), UpdatedAt: timestamppb.New(at(created)), Origin: &cli.RecordOrigin{RunId: creator, At: timestamppb.New(at(created))}}
		for _, c := range changes {
			p.Transitions = append(p.Transitions, &cli.PlanTransition{Status: cli.PlanStatus_PLAN_STATUS_OPEN, Origin: &cli.RecordOrigin{RunId: c.run, At: timestamppb.New(at(c.at))}})
		}
		return &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: p}}
	}
	facts := []*cli.TaskFact{
		plan(1, "r1", 2, change{"r1", 3}),                   // made and edited in the first window: only made
		plan(2, "r1", 2, change{"r1", 12}),                  // made early, edited later by the run
		plan(3, "r1", 12),                                   // made later
		plan(4, "r1", 30),                                   // after both
		plan(5, "r2", 4, change{"r2", 5}),                   // another run's, never touched by this one
		plan(6, "", 4, change{"", 11}),                      // a person's
		plan(7, "r2", 4, change{"r2", 5}, change{"r1", 13}), // another run's, whose status the run set later
		plan(8, "r2", 12, change{"r1", 12}),                 // made by another run in the later window and set by the run
	}
	early := factsOf(t, 0, 10, facts...)
	late := factsOf(t, 10, 20, facts...)
	if !slices.Equal(early.PlansCreated, []uint32{1, 2}) || len(early.PlansChanged) != 0 {
		t.Fatalf("early: %+v %+v", early.PlansCreated, early.PlansChanged)
	}
	if !slices.Equal(late.PlansCreated, []uint32{3}) || !slices.Equal(late.PlansChanged, []uint32{2, 7, 8}) {
		t.Fatalf("late: %+v %+v", late.PlansCreated, late.PlansChanged)
	}
}
