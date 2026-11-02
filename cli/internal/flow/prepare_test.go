package flow_test

import (
	"fmt"
	"math/rand/v2"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

func theTask() task.Task {
	return task.Task{
		Name: "kv", Version: "tv1", Backend: core.BackendCPU,
		Build:     &task.Command{Argv: []string{"make"}, Timeout: time.Minute},
		Check:     task.Command{Argv: []string{"./check"}, Timeout: time.Minute},
		Benchmark: task.Command{Argv: []string{"./bench"}, Timeout: time.Minute},
		Result:    task.Result{Protocol2: true},
		Metric:    core.Metric{Name: "ops", Direction: core.Maximize},
		Samples:   5,
	}
}

func keyFor(t *testing.T, kind core.JobKind, settings core.Settings, tk task.Task, c core.Commit, env core.Fingerprint) core.JobKey {
	t.Helper()
	k, err := core.NewJobKey(core.KeyInputs{Kind: kind, Commit: c, TaskVersion: tk.Version, Environment: env, Settings: settings, SchemaVersion: core.SchemaVersion})
	if err != nil {
		t.Fatal(err)
	}
	return k
}

func queued(t *testing.T, settings core.Settings, tk task.Task, env core.Fingerprint) core.Job {
	return core.Job{
		ID: "j9", State: core.JobQueued, Key: keyFor(t, settings.Kind(), settings, tk, commit, env),
		Spec: core.JobSpec{Commit: commit, Environment: core.EnvironmentHost, Settings: settings},
	}
}

func TestACheckJobAlwaysRunsItsCheck(t *testing.T) {
	tk := theTask()
	job := queued(t, core.CheckSettings{}, tk, print_)
	// Even with a pass already saved under its key: a check job has no result of its own otherwise.
	saved := []core.Result{{Job: "j1", Key: job.Key, Outcome: core.CheckOutcome{}}}
	p, err := flow.Prepare(flow.Starting{Job: job, Task: tk, Environment: print_, Saved: saved})
	if err != nil || p.Kind != core.JobCheck || !p.RunsCheck() || p.Build == nil {
		t.Fatalf("%+v, %v", p, err)
	}
	if err := p.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestAChangedTaskOrEnvironmentFailsAJobThatWaited(t *testing.T) {
	tk := theTask()
	job := queued(t, core.MeasureSettings{}, tk, print_)
	newer := tk
	newer.Version = "tv2"
	moved := print_
	moved.Facts = map[string]string{"os": "darwin"}
	for name, in := range map[string]flow.Starting{
		"the task":        {Job: job, Task: newer, Environment: print_},
		"the environment": {Job: job, Task: tk, Environment: moved},
	} {
		_, err := flow.Prepare(in)
		f, ok := err.(*core.Failure)
		if !ok || f.Kind != core.FailureEnvironmentMismatch || !strings.Contains(f.Message, "j9") {
			t.Errorf("%s changed: %v", name, err)
		}
	}
	for name, job := range map[string]core.Job{
		"no settings": {ID: "j1"},
		"a command":   {ID: "j1", Spec: core.JobSpec{Settings: core.CommandSettings{Argv: []string{"x"}}}},
	} {
		if _, err := flow.Prepare(flow.Starting{Job: job, Task: tk, Environment: print_}); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

// decoys are saved results that must never count: they differ from the job's in one of the
// things a result is a result of.
func decoys(t *testing.T, tk task.Task, r *rand.Rand, n int) []core.Result {
	other := tk
	other.Version = "tv-other"
	moved := print_
	moved.Facts = map[string]string{"os": "darwin"}
	otherCommit := core.Commit(strings.Repeat("cd", 20))
	var out []core.Result
	for i := 0; i < n; i++ {
		var key core.JobKey
		var res core.Result
		switch r.IntN(4) {
		case 0:
			key = keyFor(t, core.JobCheck, core.CheckSettings{}, other, commit, print_)
		case 1:
			key = keyFor(t, core.JobCheck, core.CheckSettings{}, tk, commit, moved)
		case 2:
			key = keyFor(t, core.JobCheck, core.CheckSettings{}, tk, otherCommit, print_)
		default:
			key = keyFor(t, core.JobMeasure, core.MeasureSettings{}, other, commit, print_)
			res.Outcome = core.MeasureOutcome{Samples: []float64{1, 2, 3, 4, 5, 6}}
		}
		res.Job, res.Key, res.RecordedAt = core.JobID(fmt.Sprintf("decoy%d", i)), key, epoch.Add(time.Duration(i)*time.Minute)
		if res.Outcome == nil {
			res.Outcome = core.CheckOutcome{}
		}
		out = append(out, res)
	}
	return out
}

// Over generated sets of saved results, flags and sample counts, the plan counts exactly the
// results under the job's key, links a check pass only when the rules allow one, and is always a
// plan that can be followed.
func TestAMeasureJobsPlanFollowsTheCacheRulesOverEverySavedState(t *testing.T) {
	tk := theTask()
	for seed := uint64(0); seed < 1500; seed++ {
		r := rand.New(rand.NewPCG(seed, 3))
		settings := core.MeasureSettings{Samples: uint32(r.IntN(8)), Recheck: r.IntN(3) == 0}
		noCache := r.IntN(4) == 0
		job := queued(t, settings, tk, print_)
		checkKey := keyFor(t, core.JobCheck, core.CheckSettings{}, tk, commit, print_)

		saved := decoys(t, tk, r, r.IntN(6))
		var have uint32
		var passes []core.JobID
		for i := 0; i < r.IntN(4); i++ {
			n := 1 + r.IntN(4)
			have += uint32(n)
			saved = append(saved, core.Result{
				Job: core.JobID(fmt.Sprintf("m%d", i)), Key: job.Key, RecordedAt: epoch.Add(time.Duration(i) * time.Hour),
				Outcome: core.MeasureOutcome{Samples: make([]float64, n), CheckJob: "x"},
			})
		}
		for i := 0; i < r.IntN(3); i++ {
			id := core.JobID(fmt.Sprintf("c%d", i))
			passes = append(passes, id)
			saved = append(saved, core.Result{Job: id, Key: checkKey, RecordedAt: epoch.Add(time.Duration(i) * time.Hour), Outcome: core.CheckOutcome{}})
		}
		r.Shuffle(len(saved), func(i, j int) { saved[i], saved[j] = saved[j], saved[i] })

		label := fmt.Sprintf("seed %d (%+v, no-cache %v, have %d, %d passes)", seed, settings, noCache, have, len(passes))
		p, err := flow.Prepare(flow.Starting{Job: job, NoCache: noCache, Task: tk, Environment: print_, Saved: saved})
		if err != nil {
			t.Fatalf("%s: %v", label, err)
		}
		if err := p.Validate(); err != nil {
			t.Fatalf("%s: %v", label, err)
		}
		want := settings.Samples
		if want == 0 {
			want = tk.Samples
		}
		switch {
		case noCache || have == 0 || have >= want:
			if p.Samples != want {
				t.Fatalf("%s: takes %d samples, want %d", label, p.Samples, want)
			}
		default:
			if p.Samples != want-have {
				t.Fatalf("%s: takes %d samples, want the %d missing", label, p.Samples, want-have)
			}
		}
		fresh := noCache || settings.Recheck
		switch {
		case fresh && p.CheckPass != "":
			t.Fatalf("%s: links pass %s although a fresh check was asked", label, p.CheckPass)
		case !fresh && len(passes) > 0 && p.CheckPass != passes[len(passes)-1]:
			t.Fatalf("%s: links %q, the latest saved pass is %q", label, p.CheckPass, passes[len(passes)-1])
		case !fresh && len(passes) == 0 && p.CheckPass != "":
			t.Fatalf("%s: links %q, but no pass is saved under its key", label, p.CheckPass)
		}
		if strings.HasPrefix(string(p.CheckPass), "decoy") {
			t.Fatalf("%s: linked a result that is not a pass of this commit, task version and environment", label)
		}
	}
}
