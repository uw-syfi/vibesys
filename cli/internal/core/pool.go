package core

import "slices"

// Measurement is a commit's saved measurements of one kind pooled into one, as CompareCommits
// pools them: every sample of every measure job of the commit taken in one environment, under one
// task version, scoring one metric on the same objectives. A top-up measure job adds its samples
// to what an earlier one took, so the commit has one measurement, not one per job.
type Measurement struct {
	Commit      Commit
	TaskVersion string
	Environment Fingerprint
	// Jobs are the measure jobs pooled, oldest first.
	Jobs []JobID
	// Outcome holds every job's samples and observations, in the jobs' order; its CheckJob is
	// the newest job's.
	Outcome MeasureOutcome
}

// PoolMeasurements pools the measurements among results by commit and kind, oldest first, and
// ignores check results. Two measurements of one commit that compare could not pool, because
// they differ in environment, task version, metric or objectives, stay apart.
func PoolMeasurements(results []Result) []Measurement {
	var commits []Commit
	for _, r := range results {
		if _, ok := r.Outcome.(MeasureOutcome); ok && !slices.Contains(commits, r.Commit) {
			commits = append(commits, r.Commit)
		}
	}
	var out []Measurement
	for _, commit := range commits {
		for _, r := range measurementsOf(commit, results) {
			o := r.Outcome.(MeasureOutcome)
			i := slices.IndexFunc(out, func(m Measurement) bool { return m.pools(r) })
			if i < 0 {
				out = append(out, Measurement{
					Commit: r.Commit, TaskVersion: r.TaskVersion, Environment: r.Environment,
					Outcome: MeasureOutcome{Metric: o.Metric, Objectives: o.Objectives},
				})
				i = len(out) - 1
			}
			out[i].add(r.Job, o)
		}
	}
	return out
}

// pools reports whether the result is of the measurement's kind.
func (m Measurement) pools(r Result) bool {
	o := r.Outcome.(MeasureOutcome)
	return r.Commit == m.Commit && r.TaskVersion == m.TaskVersion && r.Environment.Equal(m.Environment) &&
		o.Metric == m.Outcome.Metric && o.Objectives.Equal(m.Outcome.Objectives)
}

func (m *Measurement) add(job JobID, o MeasureOutcome) {
	m.Jobs = append(m.Jobs, job)
	m.Outcome.Samples = append(m.Outcome.Samples, o.Samples...)
	m.Outcome.CheckJob = o.CheckJob
	for _, ob := range o.Observations {
		i := slices.IndexFunc(m.Outcome.Observations, func(p Observation) bool { return p.Name == ob.Name })
		if i < 0 {
			m.Outcome.Observations = append(m.Outcome.Observations, Observation{Name: ob.Name})
			i = len(m.Outcome.Observations) - 1
		}
		m.Outcome.Observations[i].Values = append(m.Outcome.Observations[i].Values, ob.Values...)
	}
}
