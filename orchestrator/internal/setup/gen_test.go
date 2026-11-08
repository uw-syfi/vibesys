package setup_test

import (
	"fmt"
	"math/rand/v2"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/setup"
)

// The property tests here draw their cases from seeded generators, as the rest of this module does.
// Each test runs seeds 0..cases-1, so a failure names the seed that reproduces it.
const cases = 400

const (
	version = "v1"
	base    = "base"
)

var epoch = time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)

func newRand(seed uint64) *rand.Rand { return rand.New(rand.NewPCG(seed, seed^0x9e3779b97f4a7c15)) }

func pick[T any](r *rand.Rand, xs ...T) T { return xs[r.IntN(len(xs))] }

func ptr[T any](v T) *T { return &v }

// genFacts draws facts that satisfy the reader's contract: a job's relation is BASE exactly when its
// commit is the base's, only a descendant has a change, and (finished, id) is unique. Every
// base measurement has a distinct sample count, so "the one with the most samples" is unambiguous.
func genFacts(r *rand.Rand) setup.Facts {
	f := setup.Facts{Task: "t", Version: version, Base: setup.Base{Commit: base, Version: pick(r, version, version, "v0")}}
	for i := range r.IntN(4) {
		o := setup.Objective{Name: fmt.Sprintf("o%d", i), Unit: "ms"}
		if r.IntN(2) == 0 {
			o.NoiseFloor = ptr(float64(r.IntN(100)) / 1000)
		}
		f.Decl.Objectives = append(f.Decl.Objectives, o)
	}
	f.Decl.CheckDeclared, f.Decl.BenchmarkDeclared = r.IntN(4) > 0, r.IntN(4) > 0
	for i := range r.IntN(14) {
		f.Jobs = append(f.Jobs, genJob(r, i, version))
	}
	for i := range r.IntN(3) {
		f.Measurements = append(f.Measurements, genMeasurement(r, i, f.Decl))
	}
	return f
}

func genJob(r *rand.Rand, i int, v string) setup.Job {
	rel := pick(r, setup.RelationBase, setup.RelationBase, setup.RelationDescendant, setup.RelationDescendant,
		setup.RelationAncestor, setup.RelationUnrelated, setup.RelationUnknown)
	j := setup.Job{
		ID: fmt.Sprintf("j%03d", i), Kind: pick(r, setup.KindCheck, setup.KindCheck, setup.KindMeasure),
		State:    pick(r, setup.JobQueued, setup.JobRunning, setup.JobSucceeded, setup.JobSucceeded, setup.JobFailed, setup.JobFailed, setup.JobCancelled, setup.JobLost),
		Version:  v,
		Relation: rel,
		Commit:   fmt.Sprintf("c%d", r.IntN(5)),
	}
	if rel == setup.RelationBase {
		j.Commit = base
	}
	if rel == setup.RelationDescendant {
		j.CodeChanged = r.IntN(4) > 0
	}
	if j.State == setup.JobFailed {
		if r.IntN(5) > 0 {
			j.ExitCode = ptr(int32(r.IntN(3)))
		}
		j.FailedStage = pick(r, setup.StageUnrecorded, setup.StageBuild, setup.StageCheck, setup.StageCheck, setup.StageMeasure)
	}
	j.SubmittedAt = epoch.Add(time.Duration(i) * time.Minute)
	if j.State >= setup.JobSucceeded {
		j.FinishedAt = j.SubmittedAt.Add(time.Duration(r.IntN(50)) * time.Second)
	}
	return j
}

func samples(r *rand.Rand, n int, spread float64) []float64 {
	xs := make([]float64, n)
	for i := range xs {
		xs[i] = 100 * (1 + spread*(r.Float64()*2-1))
	}
	return xs
}

func genMeasurement(r *rand.Rand, i int, d setup.Declarations) setup.Measurement {
	spread := pick(r, 0.0, 0.01, 0.03, 0.1, 0.5)
	m := setup.Measurement{Commit: base, Version: version, Metric: "o0", Unit: "ms", Samples: samples(r, 2+2*i+r.IntN(2), spread)}
	switch r.IntN(8) {
	case 0:
		m.Commit = "other"
	case 1:
		m.Version = "v0"
	}
	for _, x := range m.Samples {
		m.Value += x / float64(len(m.Samples))
	}
	for _, o := range d.Objectives[min(1, len(d.Objectives)):] {
		m.Observations = append(m.Observations, setup.Series{Name: o.Name, Values: samples(r, 3+r.IntN(6), spread)})
	}
	return m
}

// garbage draws facts no reader would send: empty, non-finite, negative, contradictory.
func garbage(r *rand.Rand) setup.Facts {
	f := genFacts(r)
	for i := range f.Jobs {
		f.Jobs[i].Kind = setup.JobKind(r.IntN(5))
		f.Jobs[i].State = setup.JobState(r.IntN(9))
		f.Jobs[i].Relation = setup.Relation(r.IntN(8))
		f.Jobs[i].FailedStage = setup.Stage(r.IntN(6))
		f.Jobs[i].CodeChanged = r.IntN(2) == 0
		if r.IntN(3) == 0 {
			f.Jobs[i].ExitCode = ptr(int32(r.Uint32()))
		}
	}
	bad := []float64{0, -1, nanValue(), posInf(), -posInf(), 1e308, 5e-324}
	for i := range f.Measurements {
		for k := range f.Measurements[i].Samples {
			if r.IntN(3) == 0 {
				f.Measurements[i].Samples[k] = bad[r.IntN(len(bad))]
			}
		}
		if r.IntN(3) == 0 {
			f.Measurements[i].Samples = nil
		}
	}
	for i := range f.Decl.Objectives {
		if r.IntN(3) == 0 {
			f.Decl.Objectives[i].NoiseFloor = ptr(bad[r.IntN(len(bad))])
		}
	}
	if r.IntN(4) == 0 {
		f.Base = setup.Base{}
	}
	return f
}
