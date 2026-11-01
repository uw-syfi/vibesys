package core_test

import (
	"errors"
	"fmt"
	"math"
	"math/rand/v2"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Property tests here draw from a seeded generator, so a failure repeats exactly. The seed is the
// test's; nothing reads a clock.

// newRand returns a generator whose sequence depends only on the seed.
func newRand(seed uint64) *rand.Rand { return rand.New(rand.NewPCG(seed, seed^0x9e3779b97f4a7c15)) }

const properties = 400 // examples per property: enough to cover each branch, fast enough to run in milliseconds

// epoch is the time results are recorded from. Tests step from it, so they never read a clock.
var epoch = time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)

func at(minutes int) time.Time { return epoch.Add(time.Duration(minutes) * time.Minute) }

// commitN is a valid commit whose hash is the number.
func commitN(n int) core.Commit { return core.Commit(fmt.Sprintf("%040x", n)) }

var hostEnv = core.Fingerprint{
	Kind:    core.EnvironmentHost,
	Backend: core.BackendCPU,
	Facts:   map[string]string{"os": "linux", "arch": "amd64"},
}

var dockerEnv = core.Fingerprint{
	Kind:        core.EnvironmentDocker,
	Backend:     core.BackendCPU,
	ImageDigest: fmt.Sprintf("sha256:%064x", 1),
}

// samplesIn draws n finite samples around a level. Spread is relative to the level.
func samplesIn(r *rand.Rand, n int, level, spread float64) []float64 {
	out := make([]float64, n)
	for i := range out {
		out[i] = level * (1 + spread*(r.Float64()*2-1))
	}
	return out
}

// anySamples draws a sample set from the contract's whole input space: one to ten samples, of any
// sign, repeated or not, with a mean that can be zero, at any magnitude.
func anySamples(r *rand.Rand) []float64 {
	n := 1 + r.IntN(10)
	scale := math.Pow(10, float64(r.IntN(9)-4))
	out := make([]float64, n)
	switch r.IntN(5) {
	case 0: // all alike
		v := (r.Float64()*2 - 1) * scale
		for i := range out {
			out[i] = v
		}
	case 1: // small integers, so means can be exactly zero and values repeat
		for i := range out {
			out[i] = float64(r.IntN(7) - 3)
		}
	case 2: // signed
		for i := range out {
			out[i] = (r.Float64()*2 - 1) * scale
		}
	default: // positive with spread
		return samplesIn(r, n, scale*(0.5+r.Float64()), r.Float64()*0.3)
	}
	return out
}

func directions() []core.Direction { return []core.Direction{core.Maximize, core.Minimize} }

func opposite(d core.Direction) core.Direction {
	if d == core.Maximize {
		return core.Minimize
	}
	return core.Maximize
}

// failureKind returns the kind of the *core.Failure an error is, or "" when it is not one.
func failureKind(err error) core.FailureKind {
	var failure *core.Failure
	if errors.As(err, &failure) {
		return failure.Kind
	}
	return ""
}

func mustCompare(t testing.TB, in core.CompareInput) core.Verdict {
	t.Helper()
	verdict, err := core.Compare(in)
	if err != nil {
		t.Fatalf("Compare(%+v): %v", in, err)
	}
	return verdict
}

// A measurement of a commit: saved by job, recorded at a minute, resting on a check that ran in
// the same job. Options change what a test is about.
func measurement(job string, commit core.Commit, samples []float64, minute int, options ...func(*core.Result)) core.Result {
	r := core.Result{
		Job:           core.JobID(job),
		Key:           "key-measure",
		Task:          "kv",
		TaskVersion:   "t1",
		Commit:        commit,
		SchemaVersion: core.SchemaVersion,
		Environment:   hostEnv,
		RecordedAt:    at(minute),
		Outcome: core.MeasureOutcome{
			Metric:   core.Metric{Name: "throughput", Direction: core.Maximize},
			Samples:  samples,
			CheckJob: core.JobID(job),
		},
	}
	for _, option := range options {
		option(&r)
	}
	return r
}

// checkPass is a passed check of a commit.
func checkPass(job string, commit core.Commit, minute int, options ...func(*core.Result)) core.Result {
	r := core.Result{
		Job:           core.JobID(job),
		Key:           "key-check",
		Task:          "kv",
		TaskVersion:   "t1",
		Commit:        commit,
		SchemaVersion: core.SchemaVersion,
		Environment:   hostEnv,
		RecordedAt:    at(minute),
		Outcome:       core.CheckOutcome{},
	}
	for _, option := range options {
		option(&r)
	}
	return r
}

func inEnvironment(f core.Fingerprint) func(*core.Result) {
	return func(r *core.Result) { r.Environment = f }
}

func underTask(version string) func(*core.Result) {
	return func(r *core.Result) { r.TaskVersion = version }
}

func underKey(key core.JobKey) func(*core.Result) {
	return func(r *core.Result) { r.Key = key }
}

func scoring(m core.Metric) func(*core.Result) {
	return func(r *core.Result) {
		o := r.Outcome.(core.MeasureOutcome)
		o.Metric = m
		r.Outcome = o
	}
}

// checkedBy makes a measurement rest on another job's check pass.
func checkedBy(job string) func(*core.Result) {
	return func(r *core.Result) {
		o := r.Outcome.(core.MeasureOutcome)
		o.CheckJob = core.JobID(job)
		r.Outcome = o
	}
}

func jobIDs(results ...core.Result) []core.JobID {
	out := make([]core.JobID, len(results))
	for i, r := range results {
		out[i] = r.Job
	}
	return out
}
