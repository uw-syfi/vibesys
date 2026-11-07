package search_test

import (
	"fmt"
	"math/rand"
	"slices"
	"strings"
	"testing"
	"testing/quick"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// These tests hold the rules of the one projection of a run's measure results: what the series, the
// bounds and (in the window package) the check-in facts read. Each oracle below restates a rule in
// plain code from the contract of the facts, and shares nothing with the projection.

// span is how many whole seconds the generated times are spread over: small enough that times tie.
const span = 40

// owner is a submitter the contract accepts: a run, or no run for a person at a terminal.
func owner(run string) *cli.Submitter {
	return &cli.Submitter{RunId: run, At: &timestamppb.Timestamp{Seconds: 1}}
}

// recorded is the generated state of one measure job of a run.
type recorded struct {
	id       string
	owners   []*cli.Submitter
	finished int64 // 0 for none
	result   *cli.Result
}

// observedRun is a generated run: the job log as the store holds it, with the other runs' jobs, a
// person's, repeated results, results that cannot be placed, and bounds declared by the run, by a
// person and by another run.
type observedRun struct {
	facts []*cli.TaskFact
	jobs  []recorded
	// bounds are the declarations of the bounded objective "mem", in the order of their seq.
	bounds []*cli.ObjectiveBound
}

// observedRunFrom generates a run from a random source. The scored metric is "tput", and every result
// also observes "mem" (not a declared objective, so charted only through a bound) and "noise" (never
// an objective). The scored metric's direction changes once in a while.
func observedRunFrom(r *rand.Rand, n int) observedRun {
	var out observedRun
	pivot := n + 1
	if r.Intn(3) == 0 {
		pivot = r.Intn(n + 1)
	}
	for i := range n {
		id := fmt.Sprintf("j%03d", i)
		job := &cli.Job{Id: id}
		switch r.Intn(8) {
		case 0:
			job.Submitters = []*cli.Submitter{owner("other")}
		case 1:
			job.Submitters = []*cli.Submitter{owner("")} // a person at a terminal
		case 2:
			job.Submitters = []*cli.Submitter{owner("other"), owner("r")}
		default:
			job.Submitters = []*cli.Submitter{owner("r")}
		}
		rec := recorded{id: id, owners: job.Submitters}
		if r.Intn(3) == 0 {
			rec.finished = int64(1 + r.Intn(span))
			job.FinishedAt = &timestamppb.Timestamp{Seconds: rec.finished}
		}
		out.facts = append(out.facts, &cli.TaskFact{Fact: &cli.TaskFact_Job{Job: job}})
		if r.Intn(10) == 0 {
			out.jobs = append(out.jobs, rec)
			continue // a job with no result
		}
		direction := maximize
		if i >= pivot {
			direction = minimize
		}
		m := &cli.MeasureResult{
			Metric: &cli.Metric{Name: "tput", Direction: direction}, Value: float64(1 + r.Intn(12)),
			Observations: []*cli.Observation{{Name: "noise", Value: proto.Float64(float64(r.Intn(9)))}},
		}
		switch r.Intn(5) {
		case 0: // an observation with no values has no headline
			m.Observations = append(m.Observations, &cli.Observation{Name: "mem"})
		default:
			m.Observations = append(m.Observations, &cli.Observation{Name: "mem", Value: proto.Float64(float64(1 + r.Intn(12)))})
		}
		res := &cli.Result{JobId: id, Commit: fmt.Sprintf("c%03d", i), Outcome: &cli.Result_Measure{Measure: m}}
		if r.Intn(30) == 0 {
			res.Commit = ""
		}
		if r.Intn(5) != 0 {
			res.RecordedAt = &timestamppb.Timestamp{Seconds: int64(1 + r.Intn(span))}
		}
		rec.result = res
		out.jobs = append(out.jobs, rec)
		out.facts = append(out.facts, &cli.TaskFact{Fact: &cli.TaskFact_Result{Result: res}})
		if r.Intn(6) == 0 {
			out.facts = append(out.facts, &cli.TaskFact{Fact: &cli.TaskFact_Result{Result: proto.Clone(res).(*cli.Result)}}) // a stream repeats it
		}
	}
	for seq := 1; seq <= r.Intn(4); seq++ {
		run := []string{"r", "", "other"}[r.Intn(3)]
		direction := maximize
		if r.Intn(2) == 0 {
			direction = minimize
		}
		fact := boundFact(uint64(seq), run, "mem", direction, float64(1+r.Intn(20)))
		out.bounds = append(out.bounds, fact.GetBound())
		out.facts = append(out.facts, fact)
	}
	r.Shuffle(len(out.facts), func(i, j int) { out.facts[i], out.facts[j] = out.facts[j], out.facts[i] })
	return out
}

// standing is the direction the run's bound on "mem" stands in: the newest bound that is the run's or
// no run's. ok is false when there is none.
func (o observedRun) standing() (direction cli.Direction, ok bool) {
	for _, b := range o.bounds {
		if b.Origin.RunId == "r" || b.Origin.RunId == "" {
			direction, ok = b.Direction, true
		}
	}
	return direction, ok
}

// measuresOf are the run's measurements as the contract of the facts says to count them, oldest first:
// a measure of a job the run submitted that names its candidate and has a time, recorded or its job's.
func (o observedRun) measuresOf() []countedMeasure {
	var out []countedMeasure
	for _, rec := range o.jobs {
		res := rec.result
		if res == nil || res.Commit == "" || !slices.ContainsFunc(rec.owners, func(s *cli.Submitter) bool { return s.RunId == "r" }) {
			continue
		}
		at := rec.finished
		if res.RecordedAt != nil {
			at = res.RecordedAt.Seconds
		}
		if at == 0 {
			continue
		}
		out = append(out, countedMeasure{job: rec.id, at: at, measure: res.GetMeasure()})
	}
	slices.SortFunc(out, func(a, b countedMeasure) int {
		if a.at != b.at {
			return int(a.at - b.at)
		}
		return strings.Compare(a.job, b.job)
	})
	return out
}

type countedMeasure struct {
	job     string
	at      int64
	measure *cli.MeasureResult
}

// bests marks, in order, which values beat every earlier one in the direction, the earliest of equal
// values keeping it.
func bests(values []float64, direction cli.Direction) []bool {
	marks := make([]bool, len(values))
	for i, v := range values {
		marks[i] = true
		for _, earlier := range values[:i] {
			if (direction == maximize && earlier >= v) || (direction == minimize && earlier <= v) {
				marks[i] = false
			}
		}
	}
	return marks
}

func jobsOf(s *pb.ObjectiveSeries) []string {
	var out []string
	for _, p := range s.Points {
		out = append(out, p.JobId)
	}
	return out
}

// An observation the run (or a person outside any run) declared a bound for is charted, in the bound's
// direction, with the points of exactly the run's measurements that observed it; one that only another
// run declared a bound for is neither charted nor bounded. The bounds and the series of one state agree
// on which objectives the run has, whoever declared the bound, and on the way each improves.
func TestABoundedObservationIsChartedAndBoundedAlike(t *testing.T) {
	property := func(seed int64, size uint8) bool {
		run := observedRunFrom(rand.New(rand.NewSource(seed)), 1+int(size%40))
		s := project(t, run.facts...)
		direction, bounded := run.standing()
		series, boundsOf := seriesNamed(s, "mem"), byObjective(s, "mem")
		if bounded != (boundsOf != nil) || (bounded && boundsOf.Direction != direction) {
			t.Logf("seed %d: bounded %v, bounds %v", seed, bounded, boundsOf)
			return false
		}
		var wantJobs []string
		var wantValues []float64
		for _, m := range run.measuresOf() {
			for _, o := range m.measure.Observations {
				if o.Name == "mem" && o.Value != nil {
					wantJobs, wantValues = append(wantJobs, m.job), append(wantValues, *o.Value)
				}
			}
		}
		if !bounded || len(wantJobs) == 0 {
			return series == nil
		}
		if series == nil || series.Direction != direction || !slices.Equal(jobsOf(series), wantJobs) {
			t.Logf("seed %d: series %v, want jobs %v in %v", seed, series, wantJobs, direction)
			return false
		}
		for i, mark := range bests(wantValues, direction) {
			if series.Points[i].Value != wantValues[i] || series.Points[i].BestSoFar != mark {
				t.Logf("seed %d: point %d is %v, want %v marked %v", seed, i, series.Points[i], wantValues[i], mark)
				return false
			}
		}
		return !slices.ContainsFunc(s.Series, func(x *pb.ObjectiveSeries) bool { return x.Objective == "noise" })
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 400, Rand: rand.New(rand.NewSource(21))}); err != nil {
		t.Fatal(err)
	}
}

// The scored metric's series is the run's measurements of it in the direction its newest measurement
// says: results that cannot be placed or name no candidate are not in it, a job counts once however
// often its result repeats, a result with no time of its own is placed by its job's end, and the points
// of an older direction are left out.
func TestTheSeriesHoldsEachCountedMeasurementOnce(t *testing.T) {
	property := func(seed int64, size uint8) bool {
		run := observedRunFrom(rand.New(rand.NewSource(seed)), 1+int(size%40))
		s := project(t, run.facts...)
		counted := run.measuresOf()
		if len(counted) == 0 {
			return seriesNamed(s, "tput") == nil
		}
		newest := counted[len(counted)-1].measure.Metric.Direction
		var wantJobs []string
		var wantValues []float64
		for _, m := range counted {
			if m.measure.Metric.Direction == newest {
				wantJobs, wantValues = append(wantJobs, m.job), append(wantValues, m.measure.Value)
			}
		}
		series := seriesNamed(s, "tput")
		if series == nil || series.Direction != newest || !slices.Equal(jobsOf(series), wantJobs) {
			t.Logf("seed %d: series %v, want jobs %v", seed, series, wantJobs)
			return false
		}
		for i, mark := range bests(wantValues, newest) {
			if series.Points[i].Value != wantValues[i] || series.Points[i].BestSoFar != mark {
				return false
			}
		}
		return true
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 400, Rand: rand.New(rand.NewSource(22))}); err != nil {
		t.Fatal(err)
	}
}

// The head's value for an objective is the value the measured history holds for the same result, for the
// scored metric and for an observation alike: the head and the series read a result one way.
func TestTheHeadReadsAResultAsTheSeriesDoes(t *testing.T) {
	property := func(seed int64, size uint8, pick uint8) bool {
		run := observedRunFrom(rand.New(rand.NewSource(seed)), 1+int(size%40))
		facts := slices.Clone(run.facts)
		counted := run.measuresOf()
		if len(counted) == 0 {
			return true
		}
		// A person bounds the scored metric in the way its newest measurement says it improves.
		facts = append(facts, boundFact(100, "", "tput", counted[len(counted)-1].measure.Metric.Direction, 1000))
		head := counted[int(pick)%len(counted)]
		facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Head{Head: &cli.Head{State: &cli.Head_Current{Current: &cli.CurrentResult{
			Head: strings.Repeat("a", 40), Result: &cli.Result{JobId: head.job, Outcome: &cli.Result_Measure{Measure: head.measure}},
		}}}}})
		s := project(t, facts...)
		for _, objective := range []string{"tput", "mem"} {
			bound, series := byObjective(s, objective), seriesNamed(s, objective)
			if bound == nil || bound.HeadValue == nil {
				continue
			}
			if series == nil || series.Direction != bound.Direction {
				t.Logf("seed %d: %s head %v is bounded in %v, but is charted as %v", seed, objective, *bound.HeadValue, bound.Direction, series)
				return false
			}
			for _, p := range series.Points {
				if p.JobId == head.job && p.Value != *bound.HeadValue {
					t.Logf("seed %d: %s is %v in the series and %v for the head", seed, objective, p.Value, *bound.HeadValue)
					return false
				}
			}
		}
		return true
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 400, Rand: rand.New(rand.NewSource(23))}); err != nil {
		t.Fatal(err)
	}
}

// A person's bound on an observation charts it: the objective the report shows with a bound and no
// measurement is a bounded objective with a series.
func TestAPersonsBoundOnAnObservationChartsIt(t *testing.T) {
	m := &cli.MeasureResult{
		Metric: &cli.Metric{Name: "throughput", Direction: maximize}, Value: 10,
		Observations: []*cli.Observation{{Name: "latency_ms", Value: proto.Float64(124)}},
	}
	facts := []*cli.TaskFact{
		{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: "j1", Submitters: mine}}},
		{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: "j1", Commit: strings.Repeat("c", 40), RecordedAt: &timestamppb.Timestamp{Seconds: 1}, Outcome: &cli.Result_Measure{Measure: m}}}},
		boundFact(1, "", "latency_ms", minimize, 74),
	}
	s := project(t, facts...)
	latency := seriesNamed(s, "latency_ms")
	if latency == nil || latency.Direction != minimize || len(latency.Points) != 1 || latency.Points[0].Value != 124 || !latency.Points[0].BestSoFar {
		t.Fatalf("a bound with no run is served for latency_ms, but its measurement is not charted: %v", s.Series)
	}
}

// The scored metric has its own headline value in the series and for the head, also when the result
// lists an observation of the same name: the two readings of one result do not differ.
func TestTheScoredMetricKeepsItsHeadlineValueBesideAnObservationOfItsName(t *testing.T) {
	m := &cli.MeasureResult{
		Metric: &cli.Metric{Name: "throughput", Direction: maximize}, Value: 10,
		Observations: []*cli.Observation{{Name: "throughput", Value: proto.Float64(7)}},
	}
	result := &cli.Result{JobId: "j1", Commit: strings.Repeat("c", 40), RecordedAt: &timestamppb.Timestamp{Seconds: 1}, Outcome: &cli.Result_Measure{Measure: m}}
	s := project(t,
		&cli.TaskFact{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: "j1", Submitters: mine}}},
		&cli.TaskFact{Fact: &cli.TaskFact_Result{Result: result}},
		&cli.TaskFact{Fact: &cli.TaskFact_Head{Head: &cli.Head{State: &cli.Head_Current{Current: &cli.CurrentResult{Head: strings.Repeat("a", 40), Result: result}}}}},
		boundFact(1, "r", "throughput", maximize, 20),
	)
	series, bound := seriesNamed(s, "throughput"), byObjective(s, "throughput")
	if series.Points[0].Value != 10 || bound.GetHeadValue() != 10 {
		t.Fatalf("the series has %v and the head %v for a result scoring 10", series.Points[0].Value, bound.GetHeadValue())
	}
}

// bestAtPoint100 is a run of 500 measurements whose best is the 100th, so that the best is far from the
// newest points a cut keeps.
func bestAtPoint100() []trial {
	var trials []trial
	for i := range 500 {
		v := float64(1 + i%50)
		if i == 99 {
			v = 1000
		}
		trials = append(trials, trial{owners: mine, at: int64(i + 1), values: []float64{v}})
	}
	return trials
}

// A series cut to its limit still holds the best the run reached, and every point that set a best:
// a best that was set long before the newest points is the one a screen shows, not a point that was
// merely recent.
func TestACutSeriesStillHoldsTheBestTheRunReached(t *testing.T) {
	s := project(t, factsOf(bestAtPoint100(), []objective{{"score", maximize}})...)
	series := seriesNamed(s, "score")
	if len(series.Points) != 400 || !s.Truncated {
		t.Fatalf("kept %d points, truncated %v", len(series.Points), s.Truncated)
	}
	var marked []*pb.MeasuredPoint
	for _, p := range series.Points {
		if p.BestSoFar {
			marked = append(marked, p)
		}
	}
	if len(marked) == 0 || marked[len(marked)-1].Value != 1000 {
		t.Fatalf("the points that set a best are %v: the best of 1000 is not among them", marked)
	}
	if got := series.Points[len(series.Points)-1].At.Seconds; got != 500 {
		t.Fatalf("the newest point is at %d, want the run's newest, 500", got)
	}
}

// Whatever the run's length and values, a cut series holds the newest points that set a best before it
// holds any other, then the newest of the others; every mark is as the whole history says; and the
// best is among the points.
func TestACutSeriesKeepsItsBestPointsBeforeItsNewestOnes(t *testing.T) {
	property := func(seed int64, size uint16, minimizing bool) bool {
		direction := maximize
		if minimizing {
			direction = minimize
		}
		n := 1 + int(size%1200)
		objectives := []objective{{"score", direction}}
		trials := trialsFrom(rand.New(rand.NewSource(seed)), n, 1)
		s := project(t, factsOf(trials, objectives)...)
		series := seriesNamed(s, "score")
		byJob := slices.Clone(trials)
		slices.SortFunc(byJob, func(a, b trial) int { return int(a.at - b.at) })
		values := make([]float64, n)
		for i, tr := range byJob {
			values[i] = tr.values[0]
		}
		marks := bests(values, direction)
		var corners, others []int
		for i := range marks {
			if marks[i] {
				corners = append(corners, i)
			} else {
				others = append(others, i)
			}
		}
		var wantKept []int
		if n <= 400 {
			for i := range n {
				wantKept = append(wantKept, i)
			}
		} else {
			wantKept = slices.Clone(corners[max(0, len(corners)-400):])
			wantKept = append(wantKept, others[max(0, len(others)-(400-len(wantKept))):]...)
			slices.Sort(wantKept)
		}
		if len(series.Points) != len(wantKept) || s.Truncated != (n > 400) {
			t.Logf("seed %d: n %d, kept %d, want %d, truncated %v", seed, n, len(series.Points), len(wantKept), s.Truncated)
			return false
		}
		for k, p := range series.Points {
			i := wantKept[k]
			if p.At.Seconds != byJob[i].at || p.Value != values[i] || p.BestSoFar != marks[i] {
				t.Logf("seed %d: kept point %d is %v, want point %d at %d valued %v marked %v", seed, k, p, i, byJob[i].at, values[i], marks[i])
				return false
			}
		}
		best := byJob[corners[len(corners)-1]]
		return slices.ContainsFunc(series.Points, func(p *pb.MeasuredPoint) bool { return p.BestSoFar && p.At.Seconds == best.at })
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 40, Rand: rand.New(rand.NewSource(24))}); err != nil {
		t.Fatal(err)
	}
}

// shape is how a generated run's candidates are placed on its objectives, drawn to make the byte budget
// bite where the history is the hardest to cut well.
type shape uint8

const (
	// rising: every candidate beats the earlier on every maximized objective, so each point sets a best.
	rising shape = iota
	// earlyFrontier: a conflicting frontier first, its best points the oldest, and then candidates
	// every one of which the frontier beats.
	earlyFrontier
	// scattered: values from a small set, so there are ties and a few best points among many.
	scattered
	shapes
)

// widerun is a run of n candidates on count objectives of the shape, with the long ids and commits the
// byte budget is hardest on.
func widerun(r *rand.Rand, n, count int, form shape) []*cli.TaskFact {
	objectives := make([]objective, count)
	for i := range objectives {
		objectives[i] = objective{fmt.Sprintf("objective-%d-with-a-long-name", i), maximize}
		if i%2 == 1 {
			objectives[i].direction = minimize
		}
	}
	early := n / 5
	trials := make([]trial, n)
	for i := range trials {
		trials[i] = trial{owners: mine, at: int64(i + 1)}
		for j, o := range objectives {
			var v float64
			switch form {
			case rising:
				v = float64(i + 1)
				if o.direction == minimize {
					v = float64(n - i)
				}
			case earlyFrontier:
				switch {
				case i < early:
					v = float64(i + 1)
				case o.direction == maximize:
					v = -1
				default:
					v = float64(10 * n)
				}
			default:
				v = float64(1 + r.Intn(6) + j)
			}
			trials[i].values = append(trials[i].values, v)
		}
	}
	facts := factsOf(trials, objectives)
	for _, f := range facts {
		if res := f.GetResult(); res != nil {
			res.Commit = strings.Repeat("ab", 20)
			res.JobId = strings.Repeat("job-", 12) + res.JobId
		}
		if j := f.GetJob(); j != nil {
			j.Id = strings.Repeat("job-", 12) + j.Id
		}
	}
	return facts
}

// However the budget bites, the history keeps what matters: every objective still has its series with
// the best the run reached as its newest marked point, no point of the frontier goes while a candidate
// off it remains, and the state fits.
func TestTheByteBudgetKeepsTheBestAndTheFrontierBeforeAnythingElse(t *testing.T) {
	property := func(seed int64, size, width uint8, form shape) bool {
		n, count, form := 200+int(size%201), 2+int(width%7), form%shapes
		facts := widerun(rand.New(rand.NewSource(seed)), n, count, form)
		s := project(t, facts...)
		if proto.Size(s) > search.MaxStateBytes || len(s.Series) != count {
			t.Logf("seed %d: n %d, %d objectives, shape %d: %d bytes, %d series", seed, n, count, form, proto.Size(s), len(s.Series))
			return false
		}
		for _, series := range s.Series {
			var marked *pb.MeasuredPoint
			for _, p := range series.Points {
				if p.BestSoFar {
					marked = p
				}
			}
			extreme := bestOf(series.Direction, trialValues(facts, series.Objective))
			if marked == nil || marked.Value != extreme {
				t.Logf("seed %d: %s best is %v among %d points, want %v", seed, series.Objective, marked, len(series.Points), extreme)
				return false
			}
		}
		if s.Tradeoff == nil {
			return false
		}
		directions := make([]cli.Direction, len(s.Tradeoff.Objectives))
		for i, o := range s.Tradeoff.Objectives {
			directions[i] = o.Direction
		}
		var onFrontier, off int
		for _, p := range s.Tradeoff.Points {
			if p.OnFrontier {
				onFrontier++
			} else {
				off++
			}
		}
		frontier := frontierSize(facts, directions, s.Tradeoff.Objectives)
		if onFrontier < frontier && off > 0 {
			t.Logf("seed %d: shape %d kept %d of %d frontier candidates and %d others", seed, form, onFrontier, frontier, off)
			return false
		}
		return true
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 36, Rand: rand.New(rand.NewSource(25))}); err != nil {
		t.Fatal(err)
	}
}

// trialValues are the values of the objective of every measured candidate in the facts.
func trialValues(facts []*cli.TaskFact, name string) []float64 {
	var out []float64
	for _, f := range facts {
		m := f.GetResult().GetMeasure()
		if m == nil {
			continue
		}
		if m.Metric.Name == name {
			out = append(out, m.Value)
		}
		for _, o := range m.Observations {
			if o.Name == name {
				out = append(out, *o.Value)
			}
		}
	}
	return out
}

func bestOf(direction cli.Direction, values []float64) float64 {
	best := values[0]
	for _, v := range values {
		if (direction == maximize && v > best) || (direction == minimize && v < best) {
			best = v
		}
	}
	return best
}

// frontierSize counts the candidates of the facts that no other candidate beats on every objective.
func frontierSize(facts []*cli.TaskFact, directions []cli.Direction, objectives []*pb.TradeoffObjective) int {
	var rows [][]float64
	for _, f := range facts {
		if m := f.GetResult().GetMeasure(); m != nil {
			var row []float64
			for _, o := range objectives {
				row = append(row, trialValues([]*cli.TaskFact{f}, o.Name)[0])
			}
			rows = append(rows, row)
		}
	}
	n := 0
	for _, row := range rows {
		if !slices.ContainsFunc(rows, func(other []float64) bool { return dominatedBy(row, other, directions) }) {
			n++
		}
	}
	return n
}
