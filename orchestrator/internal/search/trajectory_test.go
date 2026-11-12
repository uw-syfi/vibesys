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

// mine says run "r" submitted a job, as a job the contract accepts.
var mine = []*cli.Submitter{{RunId: "r", At: &timestamppb.Timestamp{Seconds: 1}}}

// trial is one measured candidate: when it was recorded and its value on each objective.
type trial struct {
	owners []*cli.Submitter
	at     int64
	values []float64
}

// objective names an objective of a task and the way it improves.
type objective struct {
	name      string
	direction cli.Direction
}

// measuredFacts is the job and the measure result of one trial of a task with the objectives. The
// first objective is the metric the measurement scores; the rest are observations it declares as
// trade-off objectives (when there are two or more).
func measuredFacts(id string, t trial, objectives []objective) []*cli.TaskFact {
	m := &cli.MeasureResult{Metric: &cli.Metric{Name: objectives[0].name, Direction: objectives[0].direction}, Value: t.values[0]}
	if len(objectives) > 1 {
		m.Objectives = &cli.Objectives{}
		for _, o := range objectives {
			m.Objectives.Metrics = append(m.Objectives.Metrics, &cli.Metric{Name: o.name, Direction: o.direction})
		}
		for i, o := range objectives[1:] {
			m.Observations = append(m.Observations, &cli.Observation{Name: o.name, Value: proto.Float64(t.values[i+1])})
		}
	}
	return []*cli.TaskFact{
		{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: id, Submitters: t.owners, SubmittedAt: &timestamppb.Timestamp{Seconds: t.at}}}},
		{Fact: &cli.TaskFact_Result{Result: &cli.Result{
			JobId: id, Commit: strings.Repeat("c", 40), RecordedAt: &timestamppb.Timestamp{Seconds: t.at},
			Outcome: &cli.Result_Measure{Measure: m},
		}}},
	}
}

func seriesNamed(s *pb.SearchState, name string) *pb.ObjectiveSeries {
	for _, series := range s.Series {
		if series.Objective == name {
			return series
		}
	}
	return nil
}

// trialsFrom makes n trials of one run from a random source, with distinct times and values drawn from a
// small set so that ties happen.
func trialsFrom(r *rand.Rand, n, objectives int) []trial {
	out := make([]trial, n)
	for i := range out {
		out[i] = trial{owners: mine, at: int64(i + 1)}
		for range objectives {
			out[i].values = append(out[i].values, float64(1+r.Intn(12)))
		}
	}
	r.Shuffle(len(out), func(i, j int) { out[i], out[j] = out[j], out[i] })
	return out
}

func factsOf(trials []trial, objectives []objective) []*cli.TaskFact {
	var facts []*cli.TaskFact
	for i, t := range trials {
		facts = append(facts, measuredFacts(fmt.Sprintf("j%03d", i), t, objectives)...)
	}
	return facts
}

// A point is marked as setting a best exactly when its value beats every earlier point's in the
// objective's direction, whatever order the facts arrive in, and the last mark is the best of all.
func TestTheBestSoFarIsMarkedWhereAPointBeatsEveryEarlierOne(t *testing.T) {
	property := func(seed int64, size uint8, minimizing bool) bool {
		direction := maximize
		if minimizing {
			direction = minimize
		}
		objectives := []objective{{"score", direction}}
		trials := trialsFrom(rand.New(rand.NewSource(seed)), 1+int(size%60), 1)
		s := project(t, factsOf(trials, objectives)...)
		series := seriesNamed(s, "score")
		if series == nil || series.Direction != direction || len(series.Points) != len(trials) {
			return false
		}
		best := math.NaN()
		var lastMarked float64
		for i, p := range series.Points {
			if i > 0 && p.At.AsTime().Before(series.Points[i-1].At.AsTime()) {
				return false
			}
			beats := math.IsNaN(best) || (direction == maximize && p.Value > best) || (direction == minimize && p.Value < best)
			if p.BestSoFar != beats {
				return false
			}
			if beats {
				best, lastMarked = p.Value, p.Value
			}
		}
		extreme := series.Points[0].Value
		for _, p := range series.Points {
			if direction == maximize {
				extreme = math.Max(extreme, p.Value)
			} else {
				extreme = math.Min(extreme, p.Value)
			}
		}
		return lastMarked == extreme && s.Tradeoff == nil
	}
	if err := quick.Check(property, propcheck.Config(t, nil)); err != nil {
		t.Fatal(err)
	}
}

// dominatedBy says b is at least as good as a on every objective and better on one, written by
// turning every axis to "more is better" so it shares nothing with the projection's comparison.
func dominatedBy(a, b []float64, directions []cli.Direction) bool {
	turn := func(v []float64) []float64 {
		out := make([]float64, len(v))
		for i := range v {
			out[i] = v[i]
			if directions[i] == minimize {
				out[i] = -v[i]
			}
		}
		return out
	}
	ta, tb := turn(a), turn(b)
	atLeast, more := 0, 0
	for i := range ta {
		if tb[i] >= ta[i] {
			atLeast++
		}
		if tb[i] > ta[i] {
			more++
		}
	}
	return atLeast == len(ta) && more > 0
}

// A candidate is on the frontier exactly when no other measured candidate dominates it, for any number
// of objectives in any directions; every candidate off the frontier is dominated by one on it.
func TestTheFrontierIsWhatNoMeasuredCandidateBeats(t *testing.T) {
	property := func(seed int64, size, width uint8, flips uint8) bool {
		n, count := 1+int(size%50), 2+int(width%3)
		objectives := make([]objective, count)
		directions := make([]cli.Direction, count)
		for i := range objectives {
			directions[i] = maximize
			if flips>>i&1 == 1 {
				directions[i] = minimize
			}
			objectives[i] = objective{fmt.Sprintf("o%d", i), directions[i]}
		}
		trials := trialsFrom(rand.New(rand.NewSource(seed)), n, count)
		s := project(t, factsOf(trials, objectives)...)
		tradeoff := s.Tradeoff
		if tradeoff == nil || len(tradeoff.Objectives) != count || len(tradeoff.Points) != n {
			return false
		}
		for i, o := range tradeoff.Objectives {
			if o.Name != objectives[i].name || o.Direction != directions[i] {
				return false
			}
		}
		var frontier [][]float64
		for _, p := range tradeoff.Points {
			if p.OnFrontier {
				frontier = append(frontier, p.Values)
			}
		}
		if len(frontier) == 0 {
			return false
		}
		for _, p := range tradeoff.Points {
			beaten := slices.ContainsFunc(tradeoff.Points, func(q *pb.TradeoffPoint) bool { return dominatedBy(p.Values, q.Values, directions) })
			if p.OnFrontier == beaten {
				return false
			}
			if !p.OnFrontier && !slices.ContainsFunc(frontier, func(f []float64) bool { return dominatedBy(p.Values, f, directions) }) {
				return false
			}
		}
		// Every objective also has its own series, in the order the task declares them.
		for i, series := range s.Series {
			if series.Objective != objectives[i].name || len(series.Points) != n {
				return false
			}
		}
		return len(s.Series) == count
	}
	if err := quick.Check(property, propcheck.Config(t, nil)); err != nil {
		t.Fatal(err)
	}
}

// A task with a single objective has a series and no trade-off, and a run that measured nothing has
// neither.
func TestOneObjectiveHasNoTradeoffAndNoMeasurementHasNoSeries(t *testing.T) {
	s := project(t, measuredFacts("j1", trial{owners: mine, at: 5, values: []float64{3}}, []objective{{"score", maximize}})...)
	if s.Tradeoff != nil || len(s.Series) != 1 || s.Series[0].Points[0].JobId != "j1" || s.Series[0].Points[0].Commit != strings.Repeat("c", 40) {
		t.Fatalf("state = %v", s)
	}
	empty := project(t, boundFact(1, "r", "score", maximize, 9))
	if len(empty.Series) != 0 || empty.Tradeoff != nil {
		t.Fatalf("a run with no measurement has a history: %v", empty)
	}
}

// Another run's jobs, and results that cannot be placed in time, are not this run's measurements.
func TestOnlyThisRunsMeasuredJobsAreInTheHistory(t *testing.T) {
	objectives := []objective{{"score", maximize}}
	other := []*cli.Submitter{{RunId: "other", At: &timestamppb.Timestamp{Seconds: 1}}}
	var facts []*cli.TaskFact
	facts = append(facts, measuredFacts("mine", trial{owners: mine, at: 1, values: []float64{1}}, objectives)...)
	facts = append(facts, measuredFacts("theirs", trial{owners: other, at: 2, values: []float64{99}}, objectives)...)
	facts = append(facts, measuredFacts("both", trial{owners: append(slices.Clone(other), mine...), at: 3, values: []float64{2}}, objectives)...)
	unplaced := measuredFacts("unplaced", trial{owners: mine, at: 4, values: []float64{50}}, objectives)
	unplaced[1].GetResult().RecordedAt = nil
	facts = append(facts, unplaced...)
	s := project(t, facts...)
	var jobs []string
	for _, p := range s.Series[0].Points {
		jobs = append(jobs, p.JobId)
	}
	if !slices.Equal(jobs, []string{"mine", "both"}) {
		t.Fatalf("points = %v", jobs)
	}
}

// A reading that is not an objective is not charted; one the run declared a bound for is, in the
// bound's direction, and a declared trade-off objective is, in its own.
func TestOnlyObjectivesAreCharted(t *testing.T) {
	m := &cli.MeasureResult{
		Metric: &cli.Metric{Name: "throughput", Direction: maximize}, Value: 10,
		Observations: []*cli.Observation{
			{Name: "latency", Value: proto.Float64(40)}, {Name: "noise", Value: proto.Float64(1)}, {Name: "empty"},
		},
	}
	facts := []*cli.TaskFact{
		{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: "j1", Submitters: mine}}},
		{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: "j1", Commit: strings.Repeat("c", 40), RecordedAt: &timestamppb.Timestamp{Seconds: 1}, Outcome: &cli.Result_Measure{Measure: m}}}},
		boundFact(1, "r", "latency", minimize, 20),
	}
	s := project(t, facts...)
	var names []string
	for _, series := range s.Series {
		names = append(names, series.Objective)
	}
	if !slices.Equal(names, []string{"latency", "throughput"}) || seriesNamed(s, "latency").Direction != minimize {
		t.Fatalf("series = %v", names)
	}
	if s.Tradeoff != nil {
		t.Fatalf("one declared objective makes no trade-off: %v", s.Tradeoff)
	}
}

// When the task changes the way an objective improves, the points of the other way are left out of its
// series: the newest measurement says what the objective is.
func TestPointsOfAnotherDirectionAreLeftOut(t *testing.T) {
	var facts []*cli.TaskFact
	facts = append(facts, measuredFacts("old", trial{owners: mine, at: 1, values: []float64{5}}, []objective{{"score", minimize}})...)
	facts = append(facts, measuredFacts("new", trial{owners: mine, at: 2, values: []float64{4}}, []objective{{"score", maximize}})...)
	s := project(t, facts...)
	series := seriesNamed(s, "score")
	if series.Direction != maximize || len(series.Points) != 1 || series.Points[0].JobId != "new" || !series.Points[0].BestSoFar {
		t.Fatalf("series = %v", series)
	}
}

// More points than fit are cut from the oldest, the mark of a best keeps its meaning (it is about the
// whole history), and the state says it left something out.
func TestTheOldestPointsGoFirstAndTheStateSaysSo(t *testing.T) {
	objectives := []objective{{"score", maximize}}
	var trials []trial
	for i := range 450 {
		trials = append(trials, trial{owners: mine, at: int64(i + 1), values: []float64{float64(i)}})
	}
	s := project(t, factsOf(trials, objectives)...)
	points := s.Series[0].Points
	if len(points) != 400 || points[0].At.Seconds != 51 || !s.Truncated {
		t.Fatalf("kept %d from %v, truncated %v", len(points), points[0].At, s.Truncated)
	}
	for _, p := range points {
		if !p.BestSoFar {
			t.Fatalf("a rising series sets a best at every point: %v", p)
		}
	}
}

// A trade-off over its limit keeps every frontier candidate before it keeps a newer one that is
// beaten, so the frontier a screen draws is the frontier of the run.
func TestTheTradeoffKeepsItsFrontierWhenItIsCut(t *testing.T) {
	objectives := []objective{{"speed", maximize}, {"cost", minimize}}
	var trials []trial
	// A diagonal frontier recorded first, then 450 candidates that all lose to it.
	for i := range 20 {
		trials = append(trials, trial{owners: mine, at: int64(i + 1), values: []float64{float64(100 + i), float64(100 + i)}})
	}
	for i := range 450 {
		trials = append(trials, trial{owners: mine, at: int64(100 + i), values: []float64{1, 1000}})
	}
	s := project(t, factsOf(trials, objectives)...)
	onFrontier, total := 0, len(s.Tradeoff.Points)
	for _, p := range s.Tradeoff.Points {
		if p.OnFrontier {
			onFrontier++
		}
	}
	if total != 400 || onFrontier != 20 || !s.Truncated {
		t.Fatalf("kept %d, %d on the frontier, truncated %v", total, onFrontier, s.Truncated)
	}
}

// A measurement missing one of the declared objectives, or giving it the other way, is not a point of
// the trade-off.
func TestOnlyCandidatesMeasuredOnEveryObjectiveAreInTheTradeoff(t *testing.T) {
	objectives := []objective{{"speed", maximize}, {"cost", minimize}}
	var facts []*cli.TaskFact
	facts = append(facts, measuredFacts("both", trial{owners: mine, at: 2, values: []float64{1, 1}}, objectives)...)
	partial := measuredFacts("partial", trial{owners: mine, at: 1, values: []float64{1, 1}}, objectives)
	partial[1].GetResult().GetMeasure().Observations = nil
	facts = append(facts, partial...)
	s := project(t, facts...)
	if len(s.Tradeoff.Points) != 1 || s.Tradeoff.Points[0].JobId != "both" {
		t.Fatalf("tradeoff = %v", s.Tradeoff)
	}
	if series := seriesNamed(s, "speed"); len(series.Points) != 2 {
		t.Fatalf("the scored metric was measured twice: %v", series)
	}
}

// The state stays inside its byte budget when the history is the biggest the contract allows, the raw
// rows go first, and the history is cut by points, never by objectives.
func TestTheHistoryFitsTheByteBudgetByLosingItsOldestPoints(t *testing.T) {
	var objectives []objective
	for i := range 8 {
		objectives = append(objectives, objective{fmt.Sprintf("objective-%d-with-a-long-name", i), maximize})
	}
	var trials []trial
	for i := range 400 {
		t := trial{owners: mine, at: int64(i + 1)}
		for j := range objectives {
			t.values = append(t.values, float64(i*(j+1)))
		}
		trials = append(trials, t)
	}
	facts := factsOf(trials, objectives)
	for _, f := range facts {
		if r := f.GetResult(); r != nil {
			r.Commit = strings.Repeat("ab", 20)
			r.JobId = strings.Repeat("job-", 12) + r.JobId
		}
		if j := f.GetJob(); j != nil {
			j.Id = strings.Repeat("job-", 12) + j.Id
		}
	}
	s := project(t, facts...)
	if size := proto.Size(s); size > search.MaxStateBytes {
		t.Fatalf("state is %d bytes, over %d", size, search.MaxStateBytes)
	}
	if len(s.Series) != 8 || !s.Truncated {
		t.Fatalf("series = %d, truncated %v", len(s.Series), s.Truncated)
	}
}
