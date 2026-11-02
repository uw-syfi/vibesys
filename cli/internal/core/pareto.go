package core

import (
	"errors"
	"fmt"
	"slices"
)

// A task is multi-objective (Pareto) when its objectives.toml has a [pareto] section and declares
// more than one objective. Its measurements say so: each holds the task's Objectives, so a
// comparison is decided from what was saved and never from a task read again later. Every other
// task's measurements hold the zero Objectives and are compared on their one metric, as before.

// Objectives is what a measurement of a multi-objective task is judged on.
type Objectives struct {
	// Metrics are every objective, in the order the task declares them. The measurement's scored
	// Metric is one of them, and its samples are the measurement's Samples; the samples of every
	// other one are the Observation of its name.
	Metrics []Metric
	// RelativeNoise is the task's [pareto] relative_noise: the noise floor every objective is
	// compared under. Zero means the task declares none, and Welch's t test decides.
	RelativeNoise float64
}

// Pareto reports whether the objectives make the measurement multi-objective.
func (o Objectives) Pareto() bool { return len(o.Metrics) > 1 }

// Equal reports whether two measurements were judged on the same objectives.
func (o Objectives) Equal(p Objectives) bool {
	return slices.Equal(o.Metrics, p.Metrics) && o.RelativeNoise == p.RelativeNoise
}

// ObjectiveVerdict is the verdict on one objective of a multi-objective comparison.
type ObjectiveVerdict struct {
	Metric  Metric
	Verdict Verdict
}

// CombineObjectives is the overall verdict of a multi-objective comparison, from the verdict on
// each objective. A candidate that is inconclusive on any objective is inconclusive, with the
// reason of the first such objective, because nothing is known of how it stands there. Otherwise
// it is better when it is better on some objective and worse on none, worse when it is worse on
// some and better on none, a tradeoff when it is better on one and worse on another, and within
// noise when it is within noise on all. The verdicts are given in the order the task declares its
// objectives; only the reason depends on that order.
func CombineObjectives(verdicts []ObjectiveVerdict) (VerdictKind, InconclusiveReason) {
	var better, worse bool
	for _, v := range verdicts {
		switch v.Verdict.Kind {
		case Inconclusive:
			return Inconclusive, v.Verdict.Reason
		case Better:
			better = true
		case Worse:
			worse = true
		}
	}
	switch {
	case better && worse:
		return Tradeoff, ReasonNone
	case better:
		return Better, ReasonNone
	case worse:
		return Worse, ReasonNone
	}
	return WithinNoise, ReasonNone
}

// compareObjectives compares two non-empty sets of measurements that were all judged on the same
// objectives, objective by objective, with Compare and the objectives' noise floor.
func compareObjectives(candidate, base []Result, o Objectives) (Verdict, []ObjectiveVerdict, error) {
	scored := metricOf(candidate[0])
	verdicts := make([]ObjectiveVerdict, 0, len(o.Metrics))
	for _, m := range o.Metrics {
		c, err := objectiveSamples(candidate, m, scored)
		if err != nil {
			return Verdict{}, nil, err
		}
		b, err := objectiveSamples(base, m, scored)
		if err != nil {
			return Verdict{}, nil, err
		}
		v, err := Compare(CompareInput{Direction: m.Direction, Candidate: c, Base: b, RelativeNoise: o.RelativeNoise})
		if err != nil {
			var f *Failure
			if errors.As(err, &f) {
				return Verdict{}, nil, failf(f.Kind, "objective %q: %s", m.Name, f.Message)
			}
			return Verdict{}, nil, err
		}
		verdicts = append(verdicts, ObjectiveVerdict{Metric: m, Verdict: v})
	}
	kind, reason := CombineObjectives(verdicts)
	return Verdict{
		Kind:             kind,
		Reason:           reason,
		CandidateSamples: uint32(len(samplesOf(candidate))),
		BaseSamples:      uint32(len(samplesOf(base))),
	}, verdicts, nil
}

// objectiveSamples pools one objective's samples over measurements: the samples of the scored
// metric, and the observation of the objective's name for every other objective. A measurement
// that has no such observation fails as not measured, naming the objective, since its commit
// cannot be judged on it.
func objectiveSamples(results []Result, m Metric, scored Metric) ([]float64, error) {
	if m.Name == scored.Name {
		return samplesOf(results), nil
	}
	var out []float64
	for _, r := range results {
		values, ok := observationOf(r, m.Name)
		if !ok {
			return nil, failf(FailureNotMeasured, "the measurement %s of commit %s has no samples of the objective %q", r.Job, shortCommit(r.Commit), m.Name)
		}
		out = append(out, values...)
	}
	return out, nil
}

func observationOf(r Result, name string) ([]float64, bool) {
	for _, ob := range r.Outcome.(MeasureOutcome).Observations {
		if ob.Name == name && len(ob.Values) > 0 {
			return ob.Values, true
		}
	}
	return nil, false
}

func objectivesOf(r Result) Objectives { return r.Outcome.(MeasureOutcome).Objectives }

// ObjectiveValue is the headline number of one objective of a commit: the mean of its pooled
// samples, as Headline computes it, and how many samples it rests on.
type ObjectiveValue struct {
	Metric  Metric
	Value   float64
	Samples uint32
}

// ObjectiveValues returns the headline number of each objective of a commit's measurements, in the
// order the task declares them, pooled over every measurement of the commit as a comparison pools
// them. It is empty when the commit has no measurement or its latest measurement is not of a
// multi-objective task, and an objective no measurement has samples of is left out.
func ObjectiveValues(commit Commit, results []Result) []ObjectiveValue {
	measured := measurementsOf(commit, results)
	if len(measured) == 0 {
		return nil
	}
	latest := measured[len(measured)-1]
	o := objectivesOf(latest)
	if !o.Pareto() {
		return nil
	}
	same := slices.DeleteFunc(slices.Clone(measured), func(r Result) bool { return !objectivesOf(r).Equal(o) })
	var out []ObjectiveValue
	for _, m := range o.Metrics {
		samples, err := objectiveSamples(same, m, metricOf(latest))
		if err != nil || len(samples) == 0 {
			continue
		}
		out = append(out, ObjectiveValue{Metric: m, Value: Headline(samples), Samples: uint32(len(samples))})
	}
	return out
}

// describeObjectives is a line naming each objective's verdict, for a refusal's message.
func describeObjectives(verdicts []ObjectiveVerdict) string {
	out := ""
	for i, v := range verdicts {
		if i > 0 {
			out += ", "
		}
		out += fmt.Sprintf("%s %s", v.Metric.Name, v.Verdict.Kind)
	}
	return out
}
