package setup

import (
	"cmp"
	"fmt"
	"math"
	"slices"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// State is where one check stands. FAILED means a fact disproves the check; a read that could not be
// made, or evidence that is missing or inconclusive, is Pending.
type State uint8

// The states. Pending is the zero value.
const (
	Pending State = iota
	Running
	Passed
	Failed
)

// Policy is the part of the rules a deployment may tune.
type Policy struct {
	// MinSamples is how many readings of each objective NUMBER_STABLE needs. Values below 2 are
	// read as 2: a spread needs two readings.
	MinSamples int
	// DefaultNoise is the floor of an objective that declares none, as a fraction of its mean.
	DefaultNoise float64
}

// Standard is the policy the product uses: five samples, within 5% when the task names no floor.
var Standard = Policy{MinSamples: 5, DefaultNoise: 0.05}

// Check is one of the six checks and where it stands.
type Check struct {
	ID     core.SetupCheckID
	State  State
	Detail string
}

// Baseline is the base commit's measurement.
type Baseline struct {
	Value         float64
	Unit          string
	SpreadPercent float64
}

// Result is the six checks, in the order of Order, and the baseline once the base has a measurement.
type Result struct {
	Checks   [6]Check
	Baseline *Baseline
}

// Order is the order of the checks in a Result.
var Order = [6]core.SetupCheckID{
	core.CheckObjective, core.CheckCorrectnessCheck, core.CheckBenchmark,
	core.CheckBasePasses, core.CheckBrokenFails, core.CheckNumberStable,
}

// Unreadable is the result while the task cannot be read, such as a half-written manifest: every
// check Pending, each saying why.
func Unreadable(detail string) Result {
	var r Result
	for i, id := range Order {
		r.Checks[i] = Check{ID: id, State: Pending, Detail: detail}
	}
	return r
}

// Unmet lists the checks that have not passed, in order.
func (r Result) Unmet() []core.SetupCheckID {
	var out []core.SetupCheckID
	for _, c := range r.Checks {
		if c.State != Passed {
			out = append(out, c.ID)
		}
	}
	return out
}

// Derive reads the checks from the facts. It is pure and total: the same facts in any job order give
// the same result, and no input makes it panic.
//
// A job counts only under the working tree's task version. A check job is rejected when it FAILED
// with a nonzero exit code in the check step; a failure in another step, or in none that was
// recorded, and a cancelled or lost job prove nothing.
func Derive(f Facts, p Policy) Result {
	jobs := ordered(f.Jobs, f.Version)
	var r Result
	for i, id := range Order {
		r.Checks[i] = Check{ID: id}
	}
	set := func(id core.SetupCheckID, s State, detail string) {
		for i := range r.Checks {
			if r.Checks[i].ID == id {
				r.Checks[i].State, r.Checks[i].Detail = s, detail
			}
		}
	}

	declared(&r, f)
	base := deriveBase(jobs, f)
	set(core.CheckBasePasses, base.state, base.detail)
	broken := deriveBroken(jobs, base.state)
	set(core.CheckBrokenFails, broken.state, broken.detail)

	m, have := baseMeasurement(f)
	if have {
		r.Baseline = &Baseline{Value: m.Value, Unit: m.Unit, SpreadPercent: 100 * spreadOrZero(m.Samples)}
	}
	stable := deriveStable(f, p, m, have, measuring(jobs))
	set(core.CheckNumberStable, stable.state, stable.detail)
	return r
}

type verdict struct {
	state  State
	detail string
}

func declared(r *Result, f Facts) {
	one := func(i int, ok bool, pass, missing string) {
		if ok {
			r.Checks[i] = Check{ID: Order[i], State: Passed, Detail: pass}
		} else {
			r.Checks[i] = Check{ID: Order[i], State: Pending, Detail: missing}
		}
	}
	one(0, len(f.Decl.Objectives) > 0, fmt.Sprintf("%d declared", len(f.Decl.Objectives)), "no objective declared")
	one(1, f.Decl.CheckDeclared, "declared", "no correctness check declared")
	one(2, f.Decl.BenchmarkDeclared, "declared", "no benchmark declared")
}

// ordered keeps the jobs that count and sorts them oldest first by (finished, id), with the rest of
// the fields as tie-breakers so that no input order matters.
func ordered(jobs []Job, version string) []Job {
	var out []Job
	for _, j := range jobs {
		if j.Version == version {
			out = append(out, j)
		}
	}
	slices.SortStableFunc(out, func(a, b Job) int {
		return cmp.Or(
			a.FinishedAt.Compare(b.FinishedAt),
			cmp.Compare(a.ID, b.ID),
			cmp.Compare(a.Commit, b.Commit),
			cmp.Compare(a.State, b.State),
			cmp.Compare(a.Kind, b.Kind),
			cmp.Compare(a.Relation, b.Relation),
			cmp.Compare(a.FailedStage, b.FailedStage),
			cmp.Compare(exit(a), exit(b)),
		)
	})
	return out
}

func exit(j Job) int64 {
	if j.ExitCode == nil {
		return math.MinInt64
	}
	return int64(*j.ExitCode)
}

func live(j Job) bool { return j.State == JobQueued || j.State == JobRunning }

func rejected(j Job) bool {
	return j.State == JobFailed && j.ExitCode != nil && *j.ExitCode != 0 && j.FailedStage == StageCheck
}

func conclusive(j Job) bool { return j.State == JobSucceeded || rejected(j) }

func deriveBase(jobs []Job, f Facts) verdict {
	var latest *Job
	running := false
	for i := range jobs {
		j := jobs[i]
		if j.Kind != KindCheck || j.Relation != RelationBase {
			continue
		}
		running = running || live(j)
		if conclusive(j) {
			latest = &jobs[i]
		}
	}
	switch {
	case latest != nil && latest.State == JobSucceeded:
		return verdict{Passed, "check passed at the base"}
	case latest != nil:
		return verdict{Failed, "check rejected the base: job " + latest.ID}
	case running:
		return verdict{Running, "checking the base"}
	case f.Base.Version != f.Version:
		return verdict{Pending, "commit the task"}
	default:
		return verdict{Pending, "no check of the base yet"}
	}
}

func deriveBroken(jobs []Job, base State) verdict {
	if base != Passed {
		return verdict{Pending, "the base must pass first"}
	}
	var hole *Job
	running := false
	for i := range jobs {
		j := jobs[i]
		if j.Kind != KindCheck || j.Relation != RelationDescendant || !j.CodeChanged {
			continue
		}
		if rejected(j) {
			return verdict{Passed, "check rejected a changed copy: job " + j.ID}
		}
		running = running || live(j)
		if j.State == JobSucceeded {
			hole = &jobs[i]
		}
	}
	switch {
	case hole != nil:
		return verdict{Failed, "check accepted a changed copy at " + hole.Commit}
	case running:
		return verdict{Running, "checking a changed copy"}
	default:
		return verdict{Pending, "no changed copy checked yet"}
	}
}

func measuring(jobs []Job) bool {
	for _, j := range jobs {
		if j.Kind == KindMeasure && j.Relation == RelationBase && live(j) {
			return true
		}
	}
	return false
}

// baseMeasurement is the measurement of the base commit under the working tree's version with the
// most samples, ties broken by content so the pick never depends on order.
func baseMeasurement(f Facts) (Measurement, bool) {
	var best Measurement
	found := false
	for _, m := range f.Measurements {
		if f.Base.Commit == "" || m.Commit != f.Base.Commit || m.Version != f.Version {
			continue
		}
		if !found || measurementLess(best, m) {
			best, found = m, true
		}
	}
	return best, found
}

func measurementLess(a, b Measurement) bool {
	return cmp.Or(
		cmp.Compare(len(a.Samples), len(b.Samples)),
		cmp.Compare(a.Metric, b.Metric),
		cmp.Compare(a.Unit, b.Unit),
		cmp.Compare(a.Value, b.Value),
		slices.Compare(a.Samples, b.Samples),
		cmp.Compare(fmt.Sprint(a.Observations), fmt.Sprint(b.Observations)),
	) < 0
}

func seriesOf(m Measurement, name string) []float64 {
	if m.Metric == name {
		return m.Samples
	}
	var out []float64
	for _, s := range m.Observations {
		if s.Name == name && len(s.Values) > len(out) {
			out = s.Values
		}
	}
	return out
}

func deriveStable(f Facts, p Policy, m Measurement, have, running bool) verdict {
	wait := func(detail string) verdict {
		if running {
			return verdict{Running, "measuring the base"}
		}
		return verdict{Pending, detail}
	}
	need := max(p.MinSamples, 2)
	switch {
	case len(f.Decl.Objectives) == 0:
		return verdict{Pending, "no objective declared"}
	case !have:
		return wait("no measurement of the base yet")
	}
	for _, o := range f.Decl.Objectives {
		if n := len(seriesOf(m, o.Name)); n < need {
			return wait(fmt.Sprintf("%s: %d of %d samples", o.Name, n, need))
		}
	}
	for _, o := range f.Decl.Objectives {
		xs := seriesOf(m, o.Name)
		floor, named := p.DefaultNoise, "default floor"
		if o.NoiseFloor != nil {
			floor, named = *o.NoiseFloor, "declared floor"
		}
		if math.IsNaN(floor) || floor < 0 {
			floor, named = p.DefaultNoise, "default floor"
		}
		mean, spread, finite := stats(xs)
		switch {
		case math.IsNaN(mean) || math.IsInf(mean, 0):
			return verdict{Failed, o.Name + ": a sample is not a finite number"}
		case mean <= 0:
			return verdict{Failed, o.Name + ": the mean is not positive"}
		case !finite:
			return verdict{Failed, o.Name + ": the spread is not a finite number"}
		case spread > floor:
			return verdict{Failed, fmt.Sprintf("%s: spread %.1f%% is above the %s of %.1f%%", o.Name, 100*spread, named, 100*floor)}
		}
	}
	return verdict{Passed, fmt.Sprintf("%d objectives within their floors over %d samples", len(f.Decl.Objectives), need)}
}

// stats is the mean of xs and its sample standard deviation (n-1) over the absolute mean.
func stats(xs []float64) (mean, spread float64, finite bool) {
	n := float64(len(xs))
	sum := 0.0
	for _, x := range xs {
		sum += x
	}
	mean = sum / n
	if math.IsNaN(mean) || math.IsInf(mean, 0) {
		return mean, 0, false
	}
	ss := 0.0
	for _, x := range xs {
		d := x - mean
		ss += d * d
	}
	if len(xs) < 2 {
		return mean, 0, !math.IsInf(ss, 0)
	}
	spread = math.Sqrt(ss/(n-1)) / math.Abs(mean)
	return mean, spread, !math.IsNaN(spread) && !math.IsInf(spread, 0)
}

func spreadOrZero(xs []float64) float64 {
	if _, s, ok := stats(xs); ok {
		return s
	}
	return 0
}
