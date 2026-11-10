package search_test

import (
	"fmt"
	"math"
	"math/rand"
	"slices"
	"strings"
	"testing"
	"testing/quick"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/propcheck"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

// These tests hold the rules of the ideas a run's view derives from the task's plans. Each oracle
// restates a rule in plain code from the contract of the facts and shares nothing with the
// projection. The world is generated from a seed: plans of every status, made and changed by the run,
// by other runs and by a person, in workstreams, linked to the run's measured jobs.

const thisRun = "r"

// at is a time the contract accepts for a record's origin.
func at(seconds int64) *timestamppb.Timestamp { return &timestamppb.Timestamp{Seconds: seconds} }

func originOf(run string, seconds int64) *cli.RecordOrigin {
	return &cli.RecordOrigin{RunId: run, Agent: "a", At: at(seconds)}
}

// who is who a record names: nobody recorded, a person outside any run, this run, or another run.
type who uint8

const (
	nobody who = iota
	aPerson
	thisRunItself
	anotherRun
	whos
)

func (w who) origin(seconds int64) *cli.RecordOrigin {
	switch w {
	case aPerson:
		return originOf("", seconds)
	case thisRunItself:
		return originOf(thisRun, seconds)
	case anotherRun:
		return originOf("other", seconds)
	}
	return nil
}

// worldPlan is a generated plan with what the oracles need to know of it.
type worldPlan struct {
	plan   *cli.Plan
	madeBy who
	// setBy are who set each later status.
	setBy []who
}

// inThisRun is the rule in plain words: the run made the plan, or the run set one of its statuses.
func (p worldPlan) inThisRun() bool {
	return p.madeBy == thisRunItself || slices.Contains(p.setBy, thisRunItself)
}

var statuses = []cli.PlanStatus{
	cli.PlanStatus_PLAN_STATUS_UNSPECIFIED, cli.PlanStatus_PLAN_STATUS_OPEN, cli.PlanStatus_PLAN_STATUS_CHOSEN,
	cli.PlanStatus_PLAN_STATUS_WON, cli.PlanStatus_PLAN_STATUS_PARKED, cli.PlanStatus_PLAN_STATUS_REJECTED,
}

// job is a generated measured job: who submitted it, the plans it names, and its value.
type job struct {
	id     string
	run    string
	plans  []uint32
	values map[string]float64
	at     int64
}

// world is a generated task: plans, workstreams and the job log, as the store holds them.
type world struct {
	plans []worldPlan
	jobs  []job
	// directions are those of the objectives "tput" and "lat".
	directions map[string]cli.Direction
	facts      []*cli.TaskFact
}

// worldFrom generates a task of n plans in workstreams numbered up to streams.
func worldFrom(r *rand.Rand, n, streams, jobs int) world {
	w := world{directions: map[string]cli.Direction{"tput": maximize, "lat": minimize}}
	if r.Intn(2) == 0 {
		w.directions["tput"], w.directions["lat"] = minimize, maximize
	}
	for i := 1; i <= n; i++ {
		// Times are unrelated to who made a plan: they tie, and run backwards.
		stamp := int64(r.Intn(50))
		p := worldPlan{madeBy: who(r.Intn(int(whos)))}
		plan := &cli.Plan{
			Number: uint32(i), Title: fmt.Sprint("idea ", i), Status: statuses[r.Intn(len(statuses))],
			CreatedAt: at(stamp), UpdatedAt: at(int64(r.Intn(50))), Workstream: uint32(r.Intn(streams + 1)), Origin: p.madeBy.origin(stamp),
		}
		// A plan saved before store format 6 has no origin and, until someone changes it, no
		// transitions; a recorded plan has its creation as the first.
		if p.madeBy != nobody {
			plan.Transitions = []*cli.PlanTransition{{Status: cli.PlanStatus_PLAN_STATUS_OPEN, Origin: plan.Origin}}
		}
		for range r.Intn(4) {
			by := who(1 + r.Intn(int(whos)-1))
			p.setBy = append(p.setBy, by)
			plan.Transitions = append(plan.Transitions, &cli.PlanTransition{Status: cli.PlanStatus_PLAN_STATUS_PARKED, Origin: by.origin(int64(r.Intn(50)))})
		}
		if len(plan.Transitions) > 0 {
			plan.Status = plan.Transitions[len(plan.Transitions)-1].Status
		}
		p.plan = plan
		w.plans = append(w.plans, p)
		w.facts = append(w.facts, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: plan}})
	}
	for i := 0; i < jobs; i++ {
		j := job{id: fmt.Sprintf("job-%03d", i), run: []string{thisRun, thisRun, thisRun, "other", ""}[r.Intn(5)], at: int64(i + 1), values: map[string]float64{}}
		for range r.Intn(4) {
			if n > 0 {
				j.plans = append(j.plans, uint32(1+r.Intn(n)))
			}
		}
		for _, name := range []string{"tput", "lat"} {
			j.values[name] = float64(r.Intn(15) - 2) // some are not above zero: no ratio is stated for them
		}
		w.jobs = append(w.jobs, j)
		submitter := &cli.Submitter{RunId: j.run, At: at(1)}
		w.facts = append(w.facts, &cli.TaskFact{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: j.id, Plans: j.plans, Submitters: []*cli.Submitter{submitter}}}})
		m := &cli.MeasureResult{
			Metric: &cli.Metric{Name: "tput", Direction: w.directions["tput"]}, Value: j.values["tput"],
			Observations: []*cli.Observation{{Name: "lat", Value: proto.Float64(j.values["lat"])}},
			Objectives:   &cli.Objectives{Metrics: []*cli.Metric{{Name: "tput", Direction: w.directions["tput"]}, {Name: "lat", Direction: w.directions["lat"]}}},
		}
		w.facts = append(w.facts, &cli.TaskFact{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: j.id, Commit: fmt.Sprint("c", i), RecordedAt: at(j.at), Outcome: &cli.Result_Measure{Measure: m}}}})
	}
	r.Shuffle(len(w.facts), func(i, k int) { w.facts[i], w.facts[k] = w.facts[k], w.facts[i] })
	return w
}

func ideaOf(s *pb.SearchState, number uint32) *pb.IdeaView {
	for _, i := range s.Ideas {
		if i.PlanNumber == number {
			return i
		}
	}
	return nil
}

// oriented is the gain the contract states, in plain code: more than 1 is better.
func oriented(direction cli.Direction, best, baseline float64) float64 {
	if direction == minimize {
		return baseline / best
	}
	return best / baseline
}

// Every status a plan can have has a verdict, and a verdict is never the unset one: the mapping is
// total over the enum, a number the enum does not name, and the plan that never set one.
func TestTheVerdictMappingIsTotal(t *testing.T) {
	want := map[cli.PlanStatus]pb.IdeaVerdict{
		cli.PlanStatus_PLAN_STATUS_UNSPECIFIED: pb.IdeaVerdict_IDEA_VERDICT_OPEN,
		cli.PlanStatus_PLAN_STATUS_OPEN:        pb.IdeaVerdict_IDEA_VERDICT_OPEN,
		cli.PlanStatus_PLAN_STATUS_CHOSEN:      pb.IdeaVerdict_IDEA_VERDICT_OPEN,
		cli.PlanStatus_PLAN_STATUS_WON:         pb.IdeaVerdict_IDEA_VERDICT_KEPT,
		cli.PlanStatus_PLAN_STATUS_PARKED:      pb.IdeaVerdict_IDEA_VERDICT_PARKED,
		cli.PlanStatus_PLAN_STATUS_REJECTED:    pb.IdeaVerdict_IDEA_VERDICT_REJECTED,
	}
	values := cli.PlanStatus(0).Descriptor().Values()
	if values.Len() != len(want) {
		t.Fatalf("PlanStatus has %d values and the test knows %d: a new status needs a verdict", values.Len(), len(want))
	}
	property := func(raw int8) bool {
		status := cli.PlanStatus(raw)
		expected, known := want[status]
		if !known {
			expected = pb.IdeaVerdict_IDEA_VERDICT_OPEN // a status this build does not know
		}
		s := project(t, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: 1, Status: status}}})
		return len(s.Ideas) == 1 && s.Ideas[0].Verdict == expected
	}
	if err := quick.Check(property, propcheck.Config(t, &quick.Config{MaxCount: 300})); err != nil {
		t.Fatal(err)
	}
}

// The counts of the workstream views add up to the ideas of the run, each under its verdict and in
// its workstream, and an idea of another run is counted in none.
func TestTheCountsAddUpToTheIdeasOfTheRun(t *testing.T) {
	property := func(seed int64, size, streams uint8) bool {
		w := worldFrom(rand.New(rand.NewSource(seed)), int(size%120), int(streams%6), 0)
		s := project(t, w.facts...)
		type key struct {
			stream  uint32
			verdict pb.IdeaVerdict
		}
		want, total := map[key]uint32{}, 0
		for _, p := range w.plans {
			if p.inThisRun() {
				v := map[cli.PlanStatus]pb.IdeaVerdict{
					cli.PlanStatus_PLAN_STATUS_WON: pb.IdeaVerdict_IDEA_VERDICT_KEPT, cli.PlanStatus_PLAN_STATUS_REJECTED: pb.IdeaVerdict_IDEA_VERDICT_REJECTED,
					cli.PlanStatus_PLAN_STATUS_PARKED: pb.IdeaVerdict_IDEA_VERDICT_PARKED,
				}[p.plan.Status]
				if v == 0 {
					v = pb.IdeaVerdict_IDEA_VERDICT_OPEN
				}
				want[key{p.plan.Workstream, v}]++
				total++
			}
		}
		got, sum := map[key]uint32{}, 0
		for i, v := range s.WorkstreamViews {
			if i > 0 && v.Number <= s.WorkstreamViews[i-1].Number {
				return false // ascending, once each
			}
			got[key{v.Number, pb.IdeaVerdict_IDEA_VERDICT_KEPT}] += v.Kept
			got[key{v.Number, pb.IdeaVerdict_IDEA_VERDICT_REJECTED}] += v.Rejected
			got[key{v.Number, pb.IdeaVerdict_IDEA_VERDICT_OPEN}] += v.Open
			got[key{v.Number, pb.IdeaVerdict_IDEA_VERDICT_PARKED}] += v.Parked
			sum += int(v.Kept + v.Rejected + v.Open + v.Parked)
		}
		for k, n := range got {
			if n != want[k] {
				return false
			}
		}
		for k, n := range want {
			if got[k] != n {
				return false
			}
		}
		inRun := 0
		for _, i := range s.Ideas {
			if i.InRun {
				inRun++
			}
		}
		return sum == total && inRun == total && !s.Truncated
	}
	if err := quick.Check(property, propcheck.Config(t, &quick.Config{MaxCount: 80})); err != nil {
		t.Fatal(err)
	}
}

// M4: a plan is in the run when its recorded origin or one of its recorded status transitions names
// the run, and for no other reason: not the time it was made or edited, not a person's, another run's
// or a missing origin, and not the run's jobs that name it.
func TestAPlanIsInARunByItsRecordedOriginAndTransitions(t *testing.T) {
	property := func(seed int64, size uint8) bool {
		r := rand.New(rand.NewSource(seed))
		w := worldFrom(r, 1+int(size%60), 4, 40)
		s := project(t, w.facts...)
		for _, p := range w.plans {
			i := ideaOf(s, p.plan.Number)
			if i == nil || i.InRun != p.inThisRun() {
				t.Logf("seed %d plan %d: in_run %v, want %v (made by %d, set by %v)", seed, p.plan.Number, i.GetInRun(), p.inThisRun(), p.madeBy, p.setBy)
				return false
			}
		}
		// Neither the times nor the jobs say anything: change every time of every record, and link every
		// job of the run to every plan.
		var again []*cli.TaskFact
		for _, f := range w.facts {
			f = proto.Clone(f).(*cli.TaskFact)
			if p := f.GetPlan(); p != nil {
				p.CreatedAt, p.UpdatedAt = at(r.Int63n(1000)), at(r.Int63n(1000))
				if p.Origin != nil {
					p.Origin.At = at(r.Int63n(1000))
				}
				for _, tr := range p.Transitions {
					tr.Origin.At = at(r.Int63n(1000))
				}
			}
			if j := f.GetJob(); j != nil {
				for _, p := range w.plans {
					j.Plans = append(j.Plans, p.plan.Number)
				}
			}
			again = append(again, f)
		}
		for _, i := range project(t, again...).Ideas {
			if i.InRun != w.plans[i.PlanNumber-1].inThisRun() {
				return false
			}
		}
		return true
	}
	if err := quick.Check(property, propcheck.Config(t, &quick.Config{MaxCount: 80})); err != nil {
		t.Fatal(err)
	}
}

// The run id is never empty in a run, so a call from outside any run (no run id recorded) is in no
// run, and a run with no id matches nothing.
func TestAPlanNoRunMadeIsInNoRun(t *testing.T) {
	plan := &cli.Plan{Number: 1, Origin: originOf("", 1), Transitions: []*cli.PlanTransition{{Status: cli.PlanStatus_PLAN_STATUS_OPEN, Origin: originOf("", 1)}}}
	s := search.Cap(search.Fold([]*cli.TaskFact{{Fact: &cli.TaskFact_Plan{Plan: plan}}}), &pb.SearchState{RunId: "x"})
	if len(s.Ideas) != 1 || s.Ideas[0].InRun || len(s.WorkstreamViews) != 0 {
		t.Fatalf("a person's plan is in a run: %v %v", s.Ideas, s.WorkstreamViews)
	}
}

// The candidates of an idea are the run's measured jobs that name it, oldest first, and its gain on
// each objective is the best of them over the run's first measurement, oriented so that more than 1
// is better, whichever way the objective improves.
func TestGainsAreOrientedSoThatMoreThanOneIsBetter(t *testing.T) {
	property := func(seed int64, size uint8) bool {
		w := worldFrom(rand.New(rand.NewSource(seed)), 1+int(size%30), 3, 30)
		s := project(t, w.facts...)
		var mine []job // the run's measured jobs, in the order of time
		for _, j := range w.jobs {
			if j.run == thisRun {
				mine = append(mine, j)
			}
		}
		for _, p := range w.plans {
			var candidates []string
			best := map[string]float64{}
			for _, j := range mine {
				if !slices.Contains(j.plans, p.plan.Number) {
					continue
				}
				candidates = append(candidates, j.id)
				for name, v := range j.values {
					if was, ok := best[name]; !ok || (w.directions[name] == maximize && v > was) || (w.directions[name] == minimize && v < was) {
						best[name] = v
					}
				}
			}
			i := ideaOf(s, p.plan.Number)
			if i == nil || !slices.Equal(i.CandidateJobIds, candidates) {
				return false
			}
			stated := 0
			for name, b := range best {
				base := mine[0].values[name]
				var g *pb.ObjectiveGain
				for _, x := range i.Best {
					if x.Metric == name {
						g = x
					}
				}
				if !(b > 0 && base > 0) {
					if g != nil {
						return false // no ratio of numbers that are not above zero
					}
					continue
				}
				stated++
				want := oriented(w.directions[name], b, base)
				if g == nil || math.Abs(g.Ratio-want) > 1e-9*want || g.BestValue != b || g.BaselineValue != base {
					return false
				}
				// More than 1 exactly when the best is better than where the run began.
				beats := (w.directions[name] == maximize && b > base) || (w.directions[name] == minimize && b < base)
				if (g.Ratio > 1) != beats {
					return false
				}
			}
			if len(i.Best) != stated {
				return false
			}
		}
		return true
	}
	if err := quick.Check(property, propcheck.Config(t, &quick.Config{MaxCount: 120})); err != nil {
		t.Fatal(err)
	}
}

// An objective seen the other way gives the same gain: the values of a minimized objective are the
// reciprocals of those of a maximized one, so the ratio does not depend on the direction.
func TestAGainDoesNotDependOnTheDirectionOfTheObjective(t *testing.T) {
	property := func(seed int64, size uint8) bool {
		r := rand.New(rand.NewSource(seed))
		n := 1 + int(size%12)
		values := make([]float64, n)
		for i := range values {
			values[i] = float64(1 + r.Intn(20))
		}
		gainOf := func(direction cli.Direction, flip bool) float64 {
			var facts []*cli.TaskFact
			facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: 1}}})
			for i, v := range values {
				if flip {
					v = 1 / v
				}
				id := fmt.Sprint("j", i)
				facts = append(facts,
					&cli.TaskFact{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: id, Plans: []uint32{1}, Submitters: mine}}},
					&cli.TaskFact{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: id, Commit: "c", RecordedAt: at(int64(i + 1)), Outcome: &cli.Result_Measure{Measure: &cli.MeasureResult{Metric: &cli.Metric{Name: "x", Direction: direction}, Value: v}}}}})
			}
			s := project(t, facts...)
			if len(s.Ideas[0].Best) != 1 {
				return -1
			}
			return s.Ideas[0].Best[0].Ratio
		}
		up, down := gainOf(maximize, false), gainOf(minimize, true)
		return up > 0 && math.Abs(up-down) <= 1e-9*up
	}
	if err := quick.Check(property, propcheck.Config(t, &quick.Config{MaxCount: 100})); err != nil {
		t.Fatal(err)
	}
}

// The best of a workstream is the best gain among the ideas it counts, on each objective.
func TestAWorkstreamsBestIsTheBestGainOfItsCountedIdeas(t *testing.T) {
	property := func(seed int64, size uint8) bool {
		w := worldFrom(rand.New(rand.NewSource(seed)), 1+int(size%40), 3, 30)
		s := project(t, w.facts...)
		for _, v := range s.WorkstreamViews {
			best := map[string]float64{}
			for _, p := range w.plans {
				if p.plan.Workstream != v.Number || !p.inThisRun() {
					continue
				}
				for _, g := range ideaOf(s, p.plan.Number).Best {
					best[g.Metric] = math.Max(best[g.Metric], g.Ratio)
				}
			}
			if len(best) != len(v.Best) {
				return false
			}
			for _, g := range v.Best {
				if best[g.Metric] != g.Ratio {
					return false
				}
			}
		}
		return true
	}
	if err := quick.Check(property, propcheck.Config(t, &quick.Config{MaxCount: 80})); err != nil {
		t.Fatal(err)
	}
}

// The measured candidates that name no plan are counted apart from the jobs no run claimed.
func TestTheMeasuredCandidatesThatNameNoPlanAreCounted(t *testing.T) {
	property := func(seed int64, size uint8) bool {
		w := worldFrom(rand.New(rand.NewSource(seed)), 1+int(size%20), 3, 40)
		s := project(t, w.facts...)
		var want, unclaimed uint32
		for _, j := range w.jobs {
			if j.run == thisRun && len(j.plans) == 0 {
				want++
			}
			if j.run == "" {
				unclaimed++
			}
		}
		return s.Unplanned == want && s.UnattributedJobs == unclaimed
	}
	if err := quick.Check(property, propcheck.Config(t, &quick.Config{MaxCount: 80})); err != nil {
		t.Fatal(err)
	}
}

// Over the cap, the plans the run made, set or measured are kept before the others, newest first
// among each, the plans and their ideas stay one to one in the same order, the counts still cover
// every idea of the run, and the state says it left something out.
func TestACutKeepsTheRunsPlansBeforeTheOthers(t *testing.T) {
	property := func(seed int64, extra, ours uint8) bool {
		r := rand.New(rand.NewSource(seed))
		n := 201 + int(extra)%150
		relevant := map[uint32]bool{}
		var facts []*cli.TaskFact
		for i := 1; i <= n; i++ {
			plan := &cli.Plan{Number: uint32(i), Origin: originOf("other", 1)}
			// Mostly the run's plans are the oldest, which a cap by number would cut first.
			if i <= int(ours)%260 && r.Intn(5) > 0 {
				plan.Origin = originOf(thisRun, 1)
				relevant[uint32(i)] = true
			}
			facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: plan}})
		}
		s := project(t, facts...)
		if len(s.Plans) != 200 || len(s.Ideas) != 200 || !s.Truncated {
			return false
		}
		kept := map[uint32]bool{}
		for i, p := range s.Plans {
			if s.Ideas[i].PlanNumber != p.Number || (i > 0 && p.Number >= s.Plans[i-1].Number) {
				return false // one to one, newest first
			}
			kept[p.Number] = true
		}
		var counted uint32
		for _, v := range s.WorkstreamViews {
			counted += v.Open
		}
		if counted != uint32(len(relevant)) {
			return false
		}
		if len(relevant) <= 200 {
			for number := range relevant {
				if !kept[number] {
					return false
				}
			}
			// What the run's plans leave of the room goes to the highest numbers of the others.
			others := 200 - len(relevant)
			for number := uint32(n); number > 0 && others > 0; number-- {
				if !relevant[number] {
					others--
					if !kept[number] {
						return false
					}
				}
			}
		} else {
			for number := range kept {
				if !relevant[number] {
					return false
				}
			}
		}
		return true
	}
	if err := quick.Check(property, propcheck.Config(t, &quick.Config{MaxCount: 40})); err != nil {
		t.Fatal(err)
	}
}

// A plan the run's measured jobs name is kept before the others even when the run did not make it.
func TestAPlanTheRunMeasuredIsKeptOverTheCap(t *testing.T) {
	var facts []*cli.TaskFact
	for i := 1; i <= 300; i++ {
		facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: uint32(i)}}})
	}
	facts = append(facts,
		&cli.TaskFact{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: "j", Plans: []uint32{1}, Submitters: mine}}},
		&cli.TaskFact{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: "j", Commit: "c", RecordedAt: at(1), Outcome: &cli.Result_Measure{Measure: &cli.MeasureResult{Metric: &cli.Metric{Name: "x", Direction: maximize}, Value: 3}}}}})
	s := project(t, facts...)
	i := ideaOf(s, 1)
	if i == nil || i.InRun || !slices.Equal(i.CandidateJobIds, []string{"j"}) || s.Plans[len(s.Plans)-1].Number != 1 {
		t.Fatalf("the plan the run measured was cut: %v", i)
	}
}

// Past the limits of the contract the lists are cut and the state says so: the workstreams the run's
// ideas belong to stay, the newest of the others fill the room, and the views of an ideas of the run
// are kept to the newest workstreams.
func TestWorkstreamsAreCutToTheLimitAndTheRunsStay(t *testing.T) {
	property := func(seed int64, extra uint8) bool {
		r := rand.New(rand.NewSource(seed))
		n := 101 + int(extra)%100
		var facts []*cli.TaskFact
		used := map[uint32]bool{}
		for i := 1; i <= n; i++ {
			facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Workstream{Workstream: &cli.Workstream{Number: uint32(i), Title: fmt.Sprint("w", i), Status: cli.WorkstreamStatus_WORKSTREAM_STATUS_ACTIVE, Origin: originOf("other", 1), UpdatedAt: at(1)}}})
		}
		for k := 1; k <= 5; k++ {
			number := uint32(1 + r.Intn(n))
			used[number] = true
			facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: uint32(k), Workstream: number, Origin: originOf(thisRun, 1)}}})
		}
		s := project(t, facts...)
		if len(s.Workstreams) != 100 || !s.Truncated {
			return false
		}
		kept := map[uint32]bool{}
		for i, w := range s.Workstreams {
			if i > 0 && w.Number <= s.Workstreams[i-1].Number {
				return false
			}
			kept[w.Number] = true
		}
		for number := range used {
			if !kept[number] {
				return false
			}
		}
		return true
	}
	if err := quick.Check(property, propcheck.Config(t, &quick.Config{MaxCount: 30})); err != nil {
		t.Fatal(err)
	}
}

// The views are cut to the newest workstreams, and the ideas of none are the oldest.
func TestWorkstreamViewsAreCutToTheLimit(t *testing.T) {
	var facts []*cli.TaskFact
	for i := 0; i <= 150; i++ {
		facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: uint32(i + 1), Workstream: uint32(i), Origin: originOf(thisRun, 1)}}})
	}
	s := project(t, facts...)
	if len(s.WorkstreamViews) != 101 || s.WorkstreamViews[0].Number != 50 || !s.Truncated {
		t.Fatal(len(s.WorkstreamViews), s.WorkstreamViews[0].Number, s.Truncated)
	}
}

// However many candidates, workstreams and plans, the byte budget holds with the ideas, their views
// and the workstreams in it; a plan and its idea go together, and what goes first is the oldest.
func TestTheByteBudgetBoundsIdeasAndWorkstreams(t *testing.T) {
	property := func(seed int64, size, width uint8) bool {
		r := rand.New(rand.NewSource(seed))
		n := 30 + int(size)%170
		longID := func(i int) string { return fmt.Sprintf("%s%d", strings.Repeat("j", 240), i) }
		var facts []*cli.TaskFact
		var linked []uint32
		for i := 1; i <= n; i++ {
			linked = append(linked, uint32(i))
			facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{
				Number: uint32(i), Title: strings.Repeat("t", 100), Body: strings.Repeat("b", r.Intn(2000)), Origin: originOf(thisRun, int64(i)),
				CreatedAt: at(int64(i)), Workstream: uint32(1 + r.Intn(60)),
			}}})
		}
		for i := 0; i < 20+int(width)%180; i++ {
			// Each job names every plan, so every idea lists the run's candidates by their long ids.
			facts = append(facts,
				&cli.TaskFact{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: longID(i), Plans: linked, Submitters: mine}}},
				&cli.TaskFact{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: longID(i), Commit: strings.Repeat("c", 40), RecordedAt: at(int64(i + 1)), Outcome: &cli.Result_Measure{Measure: &cli.MeasureResult{Metric: &cli.Metric{Name: "x", Direction: maximize}, Value: float64(1 + i)}}}}})
		}
		for i := 1; i <= 100; i++ {
			facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Workstream{Workstream: &cli.Workstream{Number: uint32(i), Title: "w", Note: strings.Repeat("n", 280), Status: cli.WorkstreamStatus_WORKSTREAM_STATUS_ACTIVE, Origin: originOf(thisRun, 1), UpdatedAt: at(1)}}})
		}
		s := project(t, facts...)
		if proto.Size(s) > search.MaxStateBytes {
			return false
		}
		if len(s.Plans) != len(s.Ideas) {
			return false
		}
		for i, p := range s.Plans {
			if s.Ideas[i].PlanNumber != p.Number {
				return false
			}
		}
		// A cut state says so, and the oldest plans are the ones cut.
		if len(s.Plans) < n {
			if !s.Truncated {
				return false
			}
			for i, p := range s.Plans {
				if p.Number != uint32(len(s.Plans)-i)+uint32(n-len(s.Plans)) {
					return false
				}
			}
		}
		return true
	}
	if err := quick.Check(property, propcheck.Config(t, &quick.Config{MaxCount: 20})); err != nil {
		t.Fatal(err)
	}
}

// What the projection derives breaks none of the contract's rules, whatever the world holds.
func TestEveryProjectedIdeaHoldsTheContractsRules(t *testing.T) {
	property := func(seed int64, size uint8) bool {
		w := worldFrom(rand.New(rand.NewSource(seed)), int(size%80), 5, 40)
		project(t, w.facts...) // validates the state against protovalidate
		return true
	}
	if err := quick.Check(property, propcheck.Config(t, &quick.Config{MaxCount: 60})); err != nil {
		t.Fatal(err)
	}
}
