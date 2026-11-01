package storetest

import (
	"fmt"
	"maps"
	"math"
	"math/rand/v2"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// gen builds values of the core types from the documented input space, not from what a store
// does with them: every field set, text with the characters that trouble encodings, the extreme
// numbers, and each variant of every union. The same seed gives the same values.
type gen struct{ r *rand.Rand }

func newGen(seed uint64) *gen { return &gen{r: rand.New(rand.NewPCG(seed, 0x9e3779b97f4a7c15))} }

var texts = []string{
	"plain", "with space", "tab\tand\nnewline\r\n", `quote " backslash \ slash /`, "ünïcödé", "日本語", "emoji 🧪 and 𝄞",
	"nul\x00inside", "<tag>&amp;</tag>", "line\u2028separator\u2029", "{\"json\":[1,2]}", "# markdown\n\n- item\n", "\x7f\x1b[0m",
	strings.Repeat("long ", 2000), "   padded   ", "\ufeffbom", "Zalgo̷̢͈", "%s %d %v", "../../etc/passwd", "null",
}

func (g *gen) pick(n int) int { return g.r.IntN(n) }

// text is a non-empty string, mostly one that troubles an encoding.
func (g *gen) text() string {
	if g.pick(4) == 0 {
		return fmt.Sprintf("t%d", g.r.Uint64())
	}
	return texts[g.pick(len(texts))]
}

func (g *gen) name() string { return fmt.Sprintf("n%x", g.r.Uint32()) }

var floats = []float64{
	1, -1, 0.5, 0.1, 1.0 / 3, 1e-300, 5e-324, -5e-324, math.MaxFloat64, -math.MaxFloat64, math.SmallestNonzeroFloat64, 123456789.987654321,
	math.Copysign(0, -1), 1e21, 1e-7, 2.5e+30, 9007199254740993,
}

func (g *gen) float() float64 {
	if g.pick(2) == 0 {
		return floats[g.pick(len(floats))]
	}
	return (g.r.Float64() - 0.5) * math.Pow(10, float64(g.pick(40)-20))
}

func (g *gen) floats() []float64 {
	out := make([]float64, 1+g.pick(5))
	for i := range out {
		out[i] = g.float()
	}
	return out
}

// instant is a time in UTC to the nanosecond, from year 1 to year 9999.
func (g *gen) instant() time.Time {
	if g.pick(8) == 0 {
		return time.Date(9999, 12, 31, 23, 59, 59, 999999999, time.UTC)
	}
	sec := g.r.Int64N(253402300799+62135596800) - 62135596800
	return time.Unix(sec, g.r.Int64N(1e9)).UTC()
}

func (g *gen) u64() uint64 {
	if g.pick(3) == 0 {
		return math.MaxUint64 - uint64(g.pick(3))
	}
	return g.r.Uint64() | 1
}

func (g *gen) strs() []string {
	out := make([]string, 1+g.pick(3))
	for i := range out {
		out[i] = g.text()
	}
	return out
}

func (g *gen) facts() map[string]string {
	out := map[string]string{}
	for range 1 + g.pick(3) {
		out[g.text()] = g.text()
	}
	return out
}

func (g *gen) fingerprint() core.Fingerprint {
	return core.Fingerprint{
		Kind:        []core.EnvironmentKind{core.EnvironmentHost, core.EnvironmentDocker}[g.pick(2)],
		Backend:     []core.BackendKind{core.BackendCPU, core.BackendCUDA, core.BackendROCm, core.BackendMetal, core.BackendTrainium}[g.pick(5)],
		ImageDigest: g.text(),
		Facts:       g.facts(),
	}
}

// pinned is an environment a job can run in: host with no image, or docker with an exact digest,
// on a backend core knows, with the facts it pinned.
func (g *gen) pinned() core.Fingerprint {
	fp := core.Fingerprint{
		Kind:    core.EnvironmentHost,
		Backend: []core.BackendKind{core.BackendCPU, core.BackendCUDA, core.BackendROCm, core.BackendMetal, core.BackendTrainium}[g.pick(5)],
		Facts:   g.facts(),
	}
	if g.pick(2) == 0 {
		fp.Kind = core.EnvironmentDocker
		fp.ImageDigest = fmt.Sprintf("sha256:%016x%016x%016x%016x", g.r.Uint64(), g.r.Uint64(), g.r.Uint64(), g.r.Uint64())
	}
	return fp
}

func (g *gen) settings() core.Settings {
	switch g.pick(3) {
	case 0:
		return core.CheckSettings{}
	case 1:
		return core.CommandSettings{
			Argv:    g.strs(),
			Service: &core.ServiceSpec{Argv: g.strs(), Probe: g.strs()},
			Timeout: time.Duration(1 + g.r.Int64N(1<<40)),
		}
	}
	return core.MeasureSettings{Samples: 1 + g.r.Uint32(), Recheck: true}
}

func (g *gen) note() core.Note { return core.Note{Text: g.text(), At: g.instant(), Agent: g.text()} }

func (g *gen) spec() core.JobSpec {
	return core.JobSpec{Commit: core.Commit(g.text()), Environment: g.fingerprint().Kind, Settings: g.settings()}
}

func (g *gen) machine() core.MachineFacts {
	return core.MachineFacts{HostID: g.text(), OS: g.text(), Arch: g.text(), CPUModel: g.text(), LogicalCPUs: 1 + g.r.Uint32(), MemoryBytes: g.u64()}
}

func (g *gen) load() core.MachineLoad {
	return core.MachineLoad{SampledAt: g.instant(), LoadAverage1m: g.float(), CPUBusyFraction: g.float(), MemoryAvailableBytes: g.u64()}
}

func (g *gen) outcome(measure bool) core.Outcome {
	if !measure {
		return core.CheckOutcome{Duration: time.Duration(1 + g.r.Int64N(1<<50))}
	}
	return core.MeasureOutcome{
		Metric:       core.Metric{Name: g.text(), Direction: []core.Direction{core.Maximize, core.Minimize}[g.pick(2)]},
		Samples:      g.floats(),
		Observations: []core.Observation{{Name: g.text(), Values: g.floats()}, {Name: g.text(), Values: g.floats()}},
		CheckJob:     core.JobID(g.name()),
	}
}

// result is a saved result with every field set, for a job with this id, key and commit.
func (g *gen) result(job core.JobID, key core.JobKey, c core.Commit, measure bool) core.Result {
	return core.Result{
		Job: job, Key: key, Task: g.text(), TaskVersion: g.text(), Commit: c, SchemaVersion: 1 + g.r.Uint32(),
		Environment: g.fingerprint(), Machine: g.machine(), Load: g.load(), RecordedAt: g.instant(), Outcome: g.outcome(measure),
	}
}

// acceptedResult is a result core accepts from a job that is running in the environment it
// pinned, with every other field set at random.
func (g *gen) acceptedResult(job core.Job, measure bool) core.Result {
	r := g.result(job.ID, job.Key, job.Spec.Commit, measure)
	r.SchemaVersion = core.SchemaVersion
	r.Environment = *job.Environment
	r.Environment.Facts = maps.Clone(r.Environment.Facts)
	return r
}

func (g *gen) verdict() core.Verdict {
	return core.Verdict{
		Kind:             []core.VerdictKind{core.Better, core.Worse, core.WithinNoise, core.Inconclusive}[g.pick(4)],
		Reason:           []core.InconclusiveReason{core.ReasonTooFewSamples, core.ReasonConstantSamples, core.ReasonMetricMismatch, core.ReasonTaskVersionMismatch}[g.pick(4)],
		CandidateSamples: 1 + g.r.Uint32(), BaseSamples: 1 + g.r.Uint32(), CandidateMean: g.float(), BaseMean: g.float(), Ratio: g.float(), Noise: g.float(),
	}
}

func (g *gen) promotion() core.Promotion {
	return core.Promotion{
		Commit: core.Commit(g.text()), PreviousHead: core.Commit(g.text()), Verdict: g.verdict(), Environment: g.fingerprint(),
		ResultJob: core.JobID(g.name()), At: g.instant(),
	}
}

func (g *gen) refusal() core.PromoteRefusal {
	v := g.verdict()
	return core.PromoteRefusal{Reasons: []core.PromoteRefusalReason{
		{Kind: core.RefusalWorse, Message: g.text(), Verdict: &v, MeasuredAgainst: core.Commit(g.text()), CurrentHead: core.Commit(g.text())},
		{Kind: core.RefusalStale, Message: g.text(), Verdict: &v, MeasuredAgainst: core.Commit(g.text()), CurrentHead: core.Commit(g.text())},
	}}
}

func (g *gen) comment() core.PlanComment {
	return core.PlanComment{Text: g.text(), At: g.instant(), Agent: g.text()}
}

func (g *gen) plan() core.Plan {
	return core.Plan{
		Number: core.PlanNumber(1 + g.r.Uint32()), Title: g.text(), Body: g.text(),
		Status:   []core.PlanStatus{core.PlanOpen, core.PlanChosen, core.PlanWon, core.PlanParked, core.PlanRejected}[g.pick(5)],
		Comments: []core.PlanComment{g.comment(), g.comment()}, CreatedAt: g.instant(), UpdatedAt: g.instant(),
	}
}

// job is a job with every field set, as an event can carry it.
func (g *gen) job() core.Job {
	env, started, finished, code := g.fingerprint(), g.instant(), g.instant(), int(g.r.Int32())
	return core.Job{
		ID: core.JobID(g.name()), Spec: g.spec(), State: core.JobStates()[g.pick(6)], Key: core.JobKey(g.name()), Environment: &env,
		Notes: []core.Note{g.note(), g.note()}, Plans: []core.PlanNumber{core.PlanNumber(1 + g.r.Uint32()), 2},
		SubmittedAt: g.instant(), StartedAt: &started, FinishedAt: &finished, CancelReason: g.text(), ExitCode: &code,
		ResultJob: core.JobID(g.name()), CheckJob: core.JobID(g.name()),
	}
}

// details is one event detail of every kind a command records.
func (g *gen) details() []core.EventDetail {
	return []core.EventDetail{
		g.job(), g.result(core.JobID(g.name()), core.JobKey(g.name()), core.Commit(g.text()), true),
		g.result(core.JobID(g.name()), core.JobKey(g.name()), core.Commit(g.text()), false), g.verdict(), g.promotion(), g.refusal(),
		core.Failure{Kind: core.FailureConflict, Message: g.text()}, g.plan(), nil,
	}
}

func (g *gen) command() core.Command {
	return []core.Command{core.CommandSubmitMeasure, core.CommandWait, core.CommandPromote, core.CommandPlanComment, core.CommandDaemon}[g.pick(5)]
}

func (g *gen) draft(run core.RunID, detail core.EventDetail) core.EventDraft {
	return core.EventDraft{
		SchemaVersion: 1 + g.r.Uint32(), Run: run, HostID: g.text(), Task: g.text(), Commit: core.Commit(g.text()),
		Plan: core.PlanNumber(1 + g.pick(1000)), Harness: g.text(), Agent: g.text(), At: g.instant(), Command: g.command(),
		Outcome: []core.CommandOutcome{core.OutcomeSucceeded, core.OutcomeFailed, core.OutcomeRefused}[g.pick(3)],
		Job:     core.JobID(g.name()), Detail: detail,
	}
}

// populated fails the test for each field of v that is its zero value, so a generator that
// misses a field a core type gained is found here and not by a round trip that passes on zeros.
func populated(t *testing.T, what string, v any) {
	t.Helper()
	var walk func(path string, v reflect.Value)
	walk = func(path string, v reflect.Value) {
		switch v.Kind() {
		case reflect.Struct:
			if v.Type() == reflect.TypeOf(time.Time{}) {
				if v.IsZero() {
					t.Errorf("%s: %s is the zero time", what, path)
				}
				return
			}
			for i := range v.NumField() {
				if v.Type().Field(i).IsExported() {
					walk(path+"."+v.Type().Field(i).Name, v.Field(i))
				}
			}
		case reflect.Pointer, reflect.Interface:
			if v.IsNil() {
				t.Errorf("%s: %s is nil", what, path)
				return
			}
			walk(path, v.Elem())
		case reflect.Slice:
			if v.Len() == 0 {
				t.Errorf("%s: %s is empty", what, path)
			}
			for i := range v.Len() {
				walk(fmt.Sprintf("%s[%d]", path, i), v.Index(i))
			}
		case reflect.Map:
			if v.Len() == 0 {
				t.Errorf("%s: %s is empty", what, path)
			}
		case reflect.Float32, reflect.Float64:
			if v.Float() == 0 && !math.Signbit(v.Float()) {
				t.Errorf("%s: %s is zero", what, path)
			}
		default:
			if v.IsZero() {
				t.Errorf("%s: %s is %v", what, path, v)
			}
		}
	}
	walk("value", reflect.ValueOf(v))
}

// transitionFor is a transition of the job, at the instant, that core may allow or refuse in any
// of the ways it can: from the wrong state, to a move the state machine lacks, with an effect
// that does not belong or one that is missing, and with a result that is wrong in one field.
func (g *gen) transitionFor(job core.Job, at time.Time) core.Transition {
	states := append(core.JobStates(), "paused", "")
	tr := core.Transition{Job: job.ID, From: job.State, To: states[g.pick(len(states))], At: at}
	if g.pick(5) == 0 {
		tr.From = states[g.pick(len(states))]
	}
	if g.pick(2) == 0 { // half the moves are the ones the state machine has
		var next []core.JobState
		for _, s := range core.JobStates() {
			if job.State.CanMoveTo(s) {
				next = append(next, s)
			}
		}
		if len(next) > 0 {
			tr.To = next[g.pick(len(next))]
		}
	}
	if (tr.To == core.JobRunning && g.pick(6) != 0) || g.pick(10) == 0 {
		fp := g.pinned()
		if g.pick(8) == 0 {
			fp = core.Fingerprint{Kind: core.EnvironmentDocker, Backend: core.BackendCPU, ImageDigest: g.text()}
		}
		tr.Environment = &fp
	}
	if (tr.To == core.JobSucceeded && g.pick(6) != 0) || g.pick(12) == 0 {
		var res core.Result
		if job.Environment != nil {
			res = g.acceptedResult(job, job.Spec.Settings.Kind() == core.JobMeasure)
		} else {
			res = resultAt(job, at, 1, 2, 3)
		}
		switch g.pick(12) {
		case 0:
			res.Job = "j77"
		case 1:
			res.Key = "key-z"
		case 2:
			res.Commit = "other-commit"
		case 3:
			res.SchemaVersion++
		case 4:
			res.Environment.Facts = map[string]string{"go": "other"}
		case 5:
			res.Outcome = nil
		case 6:
			res.Outcome = core.CheckOutcome{}
		case 7:
			res.Outcome = core.MeasureOutcome{Metric: core.Metric{Name: "ops", Direction: core.Maximize}, CheckJob: "j-check"}
		case 8:
			res.Outcome = core.MeasureOutcome{Metric: core.Metric{Name: "ops", Direction: "sideways"}, Samples: []float64{1}, CheckJob: "j-check"}
		case 9:
			res.Outcome = core.MeasureOutcome{Metric: core.Metric{Name: "ops", Direction: core.Minimize}, Samples: []float64{1}}
		}
		tr.Result = &res
	}
	if g.pick(7) == 0 {
		tr.CancelReason = g.text()
	}
	if g.pick(6) == 0 {
		code := int(g.r.Int32())
		tr.ExitCode = &code
	}
	if g.pick(8) == 0 {
		tr.CheckJob = core.JobID(g.name())
	}
	if g.pick(25) == 0 {
		tr.At = time.Time{}
	}
	return tr
}
