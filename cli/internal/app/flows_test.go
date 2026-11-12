package app_test

import (
	"context"
	"errors"
	"fmt"
	"math/rand"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// The jobs below run the commands of a task, written in the fake environment's scripts. The
// benchmark writes its result to the file the flow gives it, as a real one does.

const taskDir = task.TasksDir + "/task/"

var (
	passes = []string{"exit", "0"}
	scores = []string{"emit", `{"ops": 7.5}`}
)

// withTask puts a task into the repository the tests submit to.
func (w *world) withTask(check, bench []string) {
	w.tasks.Put(string(call.Scope.Repository()), commit, tasktest.Scripted("task", check, bench))
}

func flowSpec(settings core.Settings) core.JobSpec {
	return core.JobSpec{Commit: commit, Settings: settings}
}

func submitFlow(t *testing.T, a *app.App, settings core.Settings, noCache bool) core.SubmitOutcome {
	t.Helper()
	spec := flowSpec(settings)
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec, NoCache: noCache, NoPlan: noPlan(spec)})
	if err != nil {
		t.Fatalf("Submit(%s): %v", settings.Kind(), err)
	}
	return out
}

// done submits and waits for the job to be final.
func done(t *testing.T, a *app.App, settings core.Settings, noCache bool) (core.Job, core.SubmitDisposition) {
	t.Helper()
	out := submitFlow(t, a, settings, noCache)
	return awaitFinal(t, a, out.Job.ID), out.Disposition
}

func (w *world) results(a *app.App) []core.Result {
	w.t.Helper()
	st, err := w.stores.Find(context.Background(), scope)
	if errors.Is(err, store.ErrNoStore) {
		return nil // nothing was submitted yet, so nothing is saved
	}
	if err != nil {
		w.t.Fatal(err)
	}
	got, err := st.Results(context.Background(), core.ResultFilter{})
	if err != nil {
		w.t.Fatal(err)
	}
	return got
}

// started returns how many commands that begin with the word ran.
func (w *world) started(word string) int {
	n := 0
	for _, x := range w.env.Runs() {
		if len(x.Argv) > 0 && x.Argv[0] == word {
			n++
		}
	}
	return n
}

func samplesOf(r core.Result) int {
	if m, ok := r.Outcome.(core.MeasureOutcome); ok {
		return len(m.Samples)
	}
	return 0
}

func TestACheckJobRunsTheTasksCheckAndSavesAPass(t *testing.T) {
	w := newWorld(t)
	w.withTask([]string{"print", "checking", ";", "exit", "0"}, scores)
	a := w.newApp()
	job, disposition := done(t, a, core.CheckSettings{}, false)
	if disposition != core.SubmitCreated || job.State != core.JobSucceeded {
		t.Fatalf("job = %s %s", disposition, describe(job))
	}
	if job.Key == "" || job.Environment == nil || job.Environment.Kind != core.EnvironmentHost || job.Spec.Environment != core.EnvironmentHost {
		t.Errorf("the job is not keyed or does not pin the environment it was decided to run in: %+v", job)
	}
	if got := logOf(t, a, job.ID); !strings.Contains(got, "checking") {
		t.Errorf("output = %q", got)
	}
	res := w.results(a)
	if len(res) != 1 || res[0].Job != job.ID || res[0].Key != job.Key {
		t.Fatalf("results = %+v", res)
	}
	if _, ok := res[0].Outcome.(core.CheckOutcome); !ok {
		t.Errorf("outcome = %T", res[0].Outcome)
	}
	// The same check again is answered from the pass: nothing runs.
	again, disposition := done(t, a, core.CheckSettings{}, false)
	if disposition != core.SubmitReused || again.ID != job.ID {
		t.Errorf("the second check = %s %s, want the first, reused", disposition, describe(again))
	}
	if n := w.started("print"); n != 1 {
		t.Errorf("the check ran %d times, want 1", n)
	}
	// With no-cache it runs again, and saves a second pass.
	fresh, disposition := done(t, a, core.CheckSettings{}, true)
	if disposition != core.SubmitCreated || fresh.State != core.JobSucceeded || fresh.ID == job.ID {
		t.Errorf("no-cache check = %s %s", disposition, describe(fresh))
	}
	if n := w.started("print"); n != 2 {
		t.Errorf("the check ran %d times, want 2", n)
	}
}

func TestAFailingCheckFailsTheJobAndSavesNothing(t *testing.T) {
	w := newWorld(t)
	w.withTask([]string{"print", "wrong answer", ";", "exit", "3"}, scores)
	a := w.newApp()
	job, _ := done(t, a, core.CheckSettings{}, false)
	if job.State != core.JobFailed || job.ExitCode == nil || *job.ExitCode != 3 {
		t.Fatalf("job = %s code %v", describe(job), job.ExitCode)
	}
	if res := w.results(a); len(res) != 0 {
		t.Errorf("a failed check saved %+v", res)
	}
	// A failure is never reused: the next check runs again.
	again, disposition := done(t, a, core.CheckSettings{}, false)
	if disposition != core.SubmitCreated || again.State != core.JobFailed {
		t.Errorf("the next check = %s %s", disposition, describe(again))
	}
}

func TestAMeasureJobSavesItsSamplesAndALargerOneTopsUp(t *testing.T) {
	w := newWorld(t)
	w.withTask(passes, scores)
	a := w.newApp()

	check, _ := done(t, a, core.CheckSettings{}, false)
	first, _ := done(t, a, core.MeasureSettings{Samples: 3}, false)
	if first.State != core.JobSucceeded || first.CheckJob != check.ID || first.ResultJob != first.ID {
		t.Fatalf("first measure = %s, check job %q, result job %q", describe(first), first.CheckJob, first.ResultJob)
	}
	if n := w.started("emit"); n != 3 {
		t.Fatalf("the benchmark ran %d times for 3 samples", n)
	}
	// The measure linked the check's pass and did not run the check again.
	if n := w.started("exit"); n != 1 {
		t.Errorf("the check ran %d times, want only the check job's", n)
	}
	res := w.results(a)
	if len(res) != 2 {
		t.Fatalf("results = %+v", res)
	}
	m := res[1].Outcome.(core.MeasureOutcome)
	if len(m.Samples) != 3 || m.CheckJob != check.ID || m.Samples[0] != 7.5 {
		t.Errorf("measure outcome = %+v, want 3 samples linked to check %s", m, check.ID)
	}

	// Asking for 5 takes the 2 missing samples.
	top, disposition := done(t, a, core.MeasureSettings{Samples: 5}, false)
	if disposition != core.SubmitCreated || top.State != core.JobSucceeded {
		t.Fatalf("top-up = %s %s", disposition, describe(top))
	}
	if n := w.started("emit"); n != 5 {
		t.Errorf("after the top-up the benchmark ran %d times in all, want 5", n)
	}
	res = w.results(a)
	if len(res) != 3 || samplesOf(res[2]) != 2 {
		t.Errorf("results after the top-up = %+v", res)
	}

	// Asking for no more than the first measure took runs nothing. (Whether the samples of several
	// results add up to answer a larger count is the reuse policy's to say, and is not tested here.)
	for _, n := range []uint32{1, 2, 3} {
		got, disposition := done(t, a, core.MeasureSettings{Samples: n}, false)
		if disposition != core.SubmitReused || got.State != core.JobSucceeded {
			t.Errorf("measure of %d samples = %s %s, want reused", n, disposition, describe(got))
		}
	}
	if n := w.started("emit"); n != 5 {
		t.Errorf("a reused measure ran the benchmark: %d runs in all, want 5", n)
	}
}

func TestAMeasureWithoutACheckPassRunsTheCheckItself(t *testing.T) {
	w := newWorld(t)
	w.withTask(passes, scores)
	a := w.newApp()
	job, _ := done(t, a, core.MeasureSettings{}, false)
	if job.State != core.JobSucceeded {
		t.Fatal(describe(job))
	}
	if n := w.started("exit"); n != 1 {
		t.Errorf("the check ran %d times, want 1", n)
	}
	// The sample count was the task's default, and the job says so.
	if got := job.Spec.Settings.(core.MeasureSettings).Samples; got != task.DefaultSamples {
		t.Errorf("samples = %d, want the task's default %d", got, task.DefaultSamples)
	}
	res := w.results(a)
	m := res[0].Outcome.(core.MeasureOutcome)
	if len(res) != 1 || len(m.Samples) != int(task.DefaultSamples) || m.CheckJob != job.ID {
		t.Errorf("results = %+v", res)
	}
}

func TestNoCacheAndRecheckRunTheCheckAgain(t *testing.T) {
	w := newWorld(t)
	w.withTask(passes, scores)
	a := w.newApp()
	done(t, a, core.CheckSettings{}, false)
	done(t, a, core.MeasureSettings{Samples: 2}, false)
	checks, benches := w.started("exit"), w.started("emit")
	if checks != 1 || benches != 2 {
		t.Fatalf("setup ran the check %d and the benchmark %d times", checks, benches)
	}

	// A top-up with --recheck runs the check as well as the missing samples.
	done(t, a, core.MeasureSettings{Samples: 3, Recheck: true}, false)
	if got := w.started("exit") - checks; got != 1 {
		t.Errorf("recheck ran the check %d times, want 1", got)
	}
	if got := w.started("emit") - benches; got != 1 {
		t.Errorf("recheck took %d samples, want the 1 missing", got)
	}
	checks, benches = w.started("exit"), w.started("emit")

	// A no-cache measure takes every sample again and checks afresh, though the cache is enough.
	job, disposition := done(t, a, core.MeasureSettings{Samples: 2}, true)
	if disposition != core.SubmitCreated || job.State != core.JobSucceeded {
		t.Fatalf("no-cache measure = %s %s", disposition, describe(job))
	}
	if got := w.started("exit") - checks; got != 1 {
		t.Errorf("no-cache ran the check %d times, want 1", got)
	}
	if got := w.started("emit") - benches; got != 2 {
		t.Errorf("no-cache took %d samples, want 2", got)
	}
}

func TestACancelledMeasureSavesNothing(t *testing.T) {
	for name, hold := range map[string]struct {
		check, bench []string
		marker       string
	}{
		"during the check":     {[]string{"print", "CHECKING", ";", "hold", "g"}, scores, "CHECKING"},
		"during the benchmark": {passes, []string{"print", "BENCHMARKING", ";", "hold", "g"}, "BENCHMARKING"},
	} {
		t.Run(name, func(t *testing.T) {
			w := newWorld(t)
			w.withTask(hold.check, hold.bench)
			a := w.newApp()
			job := submitFlow(t, a, core.MeasureSettings{Samples: 3}, false).Job
			awaitOutput(t, a, job.ID, hold.marker)
			got, err := a.Cancel(ctxFor(t), call, job.ID, "enough")
			if err != nil || got.State != core.JobCancelled {
				t.Fatalf("Cancel = %s, %v", describe(got), err)
			}
			if res := w.results(a); len(res) != 0 {
				t.Errorf("a cancelled measure saved %+v", res)
			}
		})
	}
}

func TestAMeasureWhoseBenchmarkReportsNothingFailsAndSavesNothing(t *testing.T) {
	for name, bench := range map[string][]string{
		"no result written":           {"exit", "0"},
		"a result that is not json":   {"emit", "junk"},
		"a result without the metric": {"emit", `{"other": 1}`},
		"a benchmark that fails":      {"exit", "4"},
	} {
		t.Run(name, func(t *testing.T) {
			w := newWorld(t)
			w.withTask(passes, bench)
			a := w.newApp()
			job, _ := done(t, a, core.MeasureSettings{Samples: 2}, false)
			if job.State != core.JobFailed {
				t.Fatalf("job = %s", describe(job))
			}
			if res := w.results(a); len(res) != 0 {
				t.Errorf("saved %+v", res)
			}
			if logOf(t, a, job.ID) == "" {
				t.Error("the job's output does not say why it failed")
			}
		})
	}
}

func TestSubmissionsTheDaemonCannotKeyAreRefusedNotQueued(t *testing.T) {
	w := newWorld(t)
	w.withTask(passes, scores)
	a := w.newApp()
	other := core.Commit(strings.Repeat("ab", 20))
	w.tasks.Put(string(call.Scope.Repository()), other, task.Tree{
		taskDir + task.ManifestName: {Data: []byte("version = \"0.1\"\nbackends = [\"cpu\"]\n[evaluator]\nname = \"x\"\n")},
	})
	junk := core.Commit(strings.Repeat("ef", 20))
	w.tasks.Put(string(call.Scope.Repository()), junk, task.Tree{
		taskDir + task.ManifestName: {Data: []byte("version = \"0.1\"\nbackends = [\"cpu\"]\nunknown_key = 1\n")},
	})
	cases := map[string]struct {
		spec core.JobSpec
		want core.FailureKind
	}{
		"a commit the repository lacks":  {core.JobSpec{Commit: core.Commit(strings.Repeat("cd", 20)), Settings: core.CheckSettings{}}, core.FailureNotFound},
		"an evaluator task":              {core.JobSpec{Commit: other, Settings: core.CheckSettings{}}, core.FailureUnavailable},
		"a task with an unknown key":     {core.JobSpec{Commit: junk, Settings: core.CheckSettings{}}, core.FailureInvalidInput},
		"docker, which is not wired":     {core.JobSpec{Commit: commit, Environment: core.EnvironmentDocker, Settings: core.MeasureSettings{Samples: 1}}, core.FailureUnavailable},
		"an environment that is not one": {core.JobSpec{Commit: commit, Environment: "vm", Settings: core.CheckSettings{}}, core.FailureInvalidInput},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: c.spec, NoPlan: noPlan(c.spec)})
			var f *core.Failure
			if !errors.As(err, &f) || f.Kind != c.want {
				t.Fatalf("Submit = %v, want %s", err, c.want)
			}
		})
	}
	jobs, err := a.Jobs(ctxFor(t), scope, core.JobFilter{})
	if err != nil || len(jobs) != 0 {
		t.Fatalf("jobs = %v, %v; a refused submission queues nothing", jobs, err)
	}
}

func TestADaemonWithoutTasksRefusesCheckAndMeasure(t *testing.T) {
	w := newWorld(t)
	w.tasks = nil
	a := w.newApp()
	for _, settings := range []core.Settings{core.CheckSettings{}, core.MeasureSettings{Samples: 3}} {
		_, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: flowSpec(settings), NoPlan: settings.Kind() == core.JobMeasure})
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
			t.Errorf("Submit(%s) = %v, want unavailable", settings.Kind(), err)
		}
	}
	// A command job reads no task.
	job := submit(t, a, "exit", "0")
	if awaitFinal(t, a, job.ID).State != core.JobSucceeded {
		t.Error("a command job did not run")
	}
}

func TestAJobWhoseTaskChangedWhileItWaitedFailsAndSavesNothing(t *testing.T) {
	w := newWorld(t)
	w.withTask(passes, scores)
	a := w.newApp()
	// The command job holds the queue, so the check waits and its task can change.
	blocker := submit(t, a, "hold", "g")
	awaitState(t, a, blocker.ID, core.JobRunning)
	waiting := submitFlow(t, a, core.CheckSettings{}, false).Job
	w.withTask([]string{"exit", "1"}, scores)
	w.gates.Release("g")
	got := awaitFinal(t, a, waiting.ID)
	if out := logOf(t, a, waiting.ID); got.State != core.JobFailed || !strings.Contains(out, "changed while") {
		t.Fatalf("job = %s, output %q", describe(got), out)
	}
	if res := w.results(a); len(res) != 0 {
		t.Errorf("saved %+v", res)
	}
	if n := w.started("exit"); n != 0 {
		t.Errorf("a stale job ran %d commands", n)
	}
}

// A daemon that dies while a measure runs leaves its supervisor to finish: the next daemon
// finds the job's result saved once, whole.
func TestAMeasureOutlivesTheDaemonThatStartedIt(t *testing.T) {
	w := newWorld(t)
	w.withTask(passes, []string{"emit", `{"ops": 7.5}`, ";", "hold", "g"})
	first := w.newApp()
	job := submitFlow(t, first, core.MeasureSettings{Samples: 2}, false).Job
	awaitState(t, first, job.ID, core.JobRunning)
	first.Close()

	second := w.newApp()
	w.gates.Release("g")
	got := awaitFinal(t, second, job.ID)
	if got.State != core.JobSucceeded {
		t.Fatalf("job = %s", describe(got))
	}
	res := w.results(second)
	if len(res) != 1 || samplesOf(res[0]) != 2 {
		t.Errorf("results = %+v", res)
	}
}

// Over any sequence of submissions the daemon never runs more than a job asks for, never less than
// it lacks, runs a check only when the cache does not hold a pass or the submission asks for a
// fresh one, and never saves a result that lacks its samples or its check.
func TestAnySequenceOfFlowSubmissionsKeepsTheCacheHonest(t *testing.T) {
	for seed := int64(1); seed <= 8; seed++ {
		t.Run(fmt.Sprintf("seed %d", seed), func(t *testing.T) {
			rng := rand.New(rand.NewSource(seed))
			w := newWorld(t)
			w.withTask(passes, scores)
			a := w.newApp()
			for step := 0; step < 14; step++ {
				noCache, recheck := rng.Intn(4) == 0, rng.Intn(3) == 0
				checks, benches := w.started("exit"), w.started("emit")
				// Every check passes here, and a job that ran one saved it: in a check result, or in the
				// measurement that ran it in its own job. So a pass is saved once any check ran.
				havePass := checks > 0
				saved := 0
				for _, r := range w.results(a) {
					saved += samplesOf(r)
				}
				var what string
				var wantChecks [2]int
				var wantBenches [2]int
				if rng.Intn(4) == 0 {
					what = fmt.Sprintf("check nocache=%v", noCache)
					job, _ := done(t, a, core.CheckSettings{}, noCache)
					if job.State != core.JobSucceeded {
						t.Fatalf("step %d, %s: %s", step, what, describe(job))
					}
					if noCache || !havePass {
						wantChecks = [2]int{1, 1}
					}
				} else {
					n := 1 + rng.Intn(6)
					what = fmt.Sprintf("measure %d nocache=%v recheck=%v", n, noCache, recheck)
					job, _ := done(t, a, core.MeasureSettings{Samples: uint32(n), Recheck: recheck}, noCache)
					if job.State != core.JobSucceeded {
						t.Fatalf("step %d, %s: %s", step, what, describe(job))
					}
					switch {
					case noCache:
						wantChecks, wantBenches = [2]int{1, 1}, [2]int{n, n}
					case saved >= n:
						// Answered from the cache. A job that still ran takes the whole count.
						wantBenches = [2]int{0, n}
						wantChecks = [2]int{0, 1}
					default:
						wantBenches = [2]int{n - saved, n}
						if recheck || !havePass {
							wantChecks = [2]int{1, 1}
						}
					}
					if wantBenches[1] > 0 && !noCache && saved >= n && (recheck || !havePass) {
						wantChecks = [2]int{0, 1}
					}
				}
				if got := w.started("exit") - checks; got < wantChecks[0] || got > wantChecks[1] {
					t.Fatalf("step %d, %s: the check ran %d times, want %v (pass saved: %v)", step, what, got, wantChecks, havePass)
				}
				if got := w.started("emit") - benches; got < wantBenches[0] || got > wantBenches[1] {
					t.Fatalf("step %d, %s: the benchmark ran %d times, want %v (had %d samples)", step, what, got, wantBenches, saved)
				}
				requireWhole(t, w.results(a))
			}
		})
	}
}

// requireWhole checks every saved result has what it is a result of.
func requireWhole(t *testing.T, results []core.Result) {
	t.Helper()
	passes := map[core.JobID]bool{}
	for _, r := range results {
		if _, ok := r.PassKey(); ok {
			passes[r.Job] = true
		}
	}
	for _, r := range results {
		m, ok := r.Outcome.(core.MeasureOutcome)
		if !ok {
			continue
		}
		if len(m.Samples) == 0 {
			t.Errorf("result of %s has no samples", r.Job)
		}
		if m.CheckJob != r.Job && !passes[m.CheckJob] {
			t.Errorf("result of %s links the check %q, which has no saved pass", r.Job, m.CheckJob)
		}
	}
}

// A measure job that ran its check saved a pass with its measurement: a check afterwards is
// answered by it and runs nothing, and a measure that needs more samples links it.
func TestAMeasureJobsCheckPassAnswersALaterCheckAndLaterMeasures(t *testing.T) {
	w := newWorld(t)
	w.withTask(passes, scores)
	a := w.newApp()
	measured, _ := done(t, a, core.MeasureSettings{Samples: 2}, false)
	if measured.State != core.JobSucceeded || w.started("exit") != 1 {
		t.Fatalf("setup: %s, the check ran %d times", describe(measured), w.started("exit"))
	}

	check, disposition := done(t, a, core.CheckSettings{}, false)
	if disposition != core.SubmitReused || check.ID != measured.ID {
		t.Errorf("a check after the measure = %s %s, want the measure job, reused", disposition, describe(check))
	}
	if n := w.started("exit"); n != 1 {
		t.Errorf("the check ran %d times in all, want the one the measure ran", n)
	}

	// A larger measure takes only the missing samples and links the pass instead of checking again.
	more, _ := done(t, a, core.MeasureSettings{Samples: 3}, false)
	if more.State != core.JobSucceeded || more.CheckJob != measured.ID || w.started("exit") != 1 || w.started("emit") != 3 {
		t.Errorf("the top-up = %s, links check %q; the check ran %d times, the benchmark %d", describe(more), more.CheckJob, w.started("exit"), w.started("emit"))
	}
	requireWhole(t, w.results(a))

	// Asking for a check with no-cache runs it, as it does for every kind.
	fresh, disposition := done(t, a, core.CheckSettings{}, true)
	if disposition != core.SubmitCreated || fresh.ID == measured.ID || w.started("exit") != 2 {
		t.Errorf("a no-cache check = %s %s; the check ran %d times, want 2", disposition, describe(fresh), w.started("exit"))
	}
}

// A job's key is made of what its result is a result of: the job kind, the commit, the task version,
// the environment fingerprint, the settings and the schema version. The test derives the key from
// those facts, read through the public interfaces, and expects the daemon's to be the same; and it
// expects every fact that differs to give another key.
func TestAJobsKeyIsMadeOfItsKindCommitTaskVersionEnvironmentSettingsAndSchema(t *testing.T) {
	other := core.Commit(strings.Repeat("cd", 20))
	otherEnv := core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"os": "other", "arch": "fake"}}
	type variant struct {
		name     string
		commit   core.Commit
		check    []string
		print    *core.Fingerprint
		settings core.Settings
	}
	variants := []variant{
		{name: "base", commit: commit, check: passes, settings: core.CheckSettings{}},
		{name: "another commit", commit: other, check: passes, settings: core.CheckSettings{}},
		{name: "another task version", commit: commit, check: []string{"exit", "0", ";", "exit", "0"}, settings: core.CheckSettings{}},
		{name: "another environment", commit: commit, check: passes, print: &otherEnv, settings: core.CheckSettings{}},
		{name: "another kind", commit: commit, check: passes, settings: core.MeasureSettings{Samples: 2}},
	}
	seen := map[core.JobKey]string{}
	for _, v := range variants {
		w := newWorld(t)
		if v.print != nil {
			w.env = fakeenv.New(core.EnvironmentHost, w.gates, fakeenv.WithFiles(w.files), fakeenv.WithFingerprint(*v.print))
		}
		w.tasks.Put(string(call.Scope.Repository()), v.commit, tasktest.Scripted("task", v.check, scores))
		a := w.newApp()
		spec := core.JobSpec{Commit: v.commit, Settings: v.settings}
		out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec, NoPlan: noPlan(spec)})
		if err != nil {
			t.Fatalf("%s: %v", v.name, err)
		}
		got := awaitFinal(t, a, out.Job.ID)

		tk, err := w.tasks.Task(context.Background(), string(call.Scope.Repository()), v.commit, string(call.Scope.Task()))
		if err != nil {
			t.Fatal(err)
		}
		print, err := w.env.Fingerprint(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		want, err := core.NewJobKey(core.KeyInputs{
			Kind: v.settings.Kind(), Commit: v.commit, TaskVersion: tk.Version,
			Environment: print, Settings: v.settings, SchemaVersion: core.SchemaVersion,
		})
		if err != nil {
			t.Fatal(err)
		}
		if got.Key != want {
			t.Errorf("%s: key %s, want %s made of the kind, commit, task version %q, environment and schema version", v.name, got.Key, want, tk.Version)
		}
		if first, dup := seen[got.Key]; dup {
			t.Errorf("%s has the key of %s", v.name, first)
		}
		seen[got.Key] = v.name
	}

	// The settings that change how many samples a measure takes, and whether it checks again, are
	// not in the key: the cache tops up a measurement, and results of any count share one key.
	w := newWorld(t)
	w.withTask(passes, scores)
	a := w.newApp()
	var keys []core.JobKey
	for _, s := range []core.MeasureSettings{{Samples: 2}, {Samples: 3}, {Samples: 3, Recheck: true}} {
		job, _ := done(t, a, s, false)
		keys = append(keys, job.Key)
	}
	if keys[0] != keys[1] || keys[1] != keys[2] {
		t.Errorf("measure keys %v, want one key for every sample count and recheck", keys)
	}
}

// A candidate whose task files differ from the base's, the commit the result branch is at, is
// refused before any job exists, with a message that names the paths. Generated files under the
// task do not count: a rewritten bytecode file is not a change to the task.
func TestACandidateThatChangesTheTaskIsRefusedAndNamesThePaths(t *testing.T) {
	repo := string(call.Scope.Repository())
	candidate := func(n int) core.Commit { return core.Commit(strings.Repeat(fmt.Sprintf("%x", n), 40)) }
	withFiles := func(edit func(task.Tree)) task.Tree {
		tree := tasktest.Scripted("task", passes, scores)
		tree[taskDir+"check.py"] = task.File{Data: []byte("assert True")}
		tree[taskDir+"data/in.txt"] = task.File{Data: []byte("1")}
		tree["main.py"] = task.File{Data: []byte("x = 1")}
		if edit != nil {
			edit(tree)
		}
		return tree
	}
	cases := map[string]struct {
		edit    func(task.Tree)
		refused []string // paths the message must name; none means accepted
	}{
		"nothing changed": {edit: nil},
		"code outside the task changed": {edit: func(tr task.Tree) {
			tr["main.py"] = task.File{Data: []byte("x = 2")}
		}},
		"bytecode rewritten": {edit: func(tr task.Tree) {
			tr[taskDir+"__pycache__/check.cpython-312.pyc"] = task.File{Data: []byte("new bytes")}
			tr[taskDir+"data/__pycache__/x.pyc"] = task.File{Data: []byte("other")}
		}},
		"checker edited": {
			edit:    func(tr task.Tree) { tr[taskDir+"check.py"] = task.File{Data: []byte("assert False")} },
			refused: []string{taskDir + "check.py (changed)"},
		},
		"file added": {
			edit:    func(tr task.Tree) { tr[taskDir+"extra.py"] = task.File{Data: []byte("")} },
			refused: []string{taskDir + "extra.py (added)"},
		},
		"file removed": {
			edit:    func(tr task.Tree) { delete(tr, taskDir+"data/in.txt") },
			refused: []string{taskDir + "data/in.txt (removed)"},
		},
		"bytecode and checker": {
			edit: func(tr task.Tree) {
				tr[taskDir+"__pycache__/a.pyc"] = task.File{Data: []byte("a")}
				tr[taskDir+"check.py"] = task.File{Data: []byte("assert False")}
			},
			refused: []string{taskDir + "check.py (changed)"},
		},
	}
	for name, c := range cases {
		for _, settings := range []core.Settings{core.CheckSettings{}, core.MeasureSettings{Samples: 3}} {
			t.Run(name+"/"+string(settings.Kind()), func(t *testing.T) {
				w := newWorld(t)
				w.branches = gitrepo.NewFakeBranches()
				w.branches.AddBranch(repo, "vibesys/result/task", commit)
				w.tasks.Put(repo, commit, withFiles(nil))
				w.tasks.Put(repo, candidate(1), withFiles(c.edit))
				a := w.newApp()

				spec := core.JobSpec{Commit: candidate(1), Settings: settings}
				_, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec, NoPlan: noPlan(spec)})
				if len(c.refused) == 0 {
					if err != nil {
						t.Fatalf("Submit = %v, want accepted", err)
					}
					return
				}
				f := wantFailure(t, "Submit", err, core.FailureConflict)
				for _, path := range c.refused {
					if !strings.Contains(f.Message, path) {
						t.Errorf("message %q does not name %q", f.Message, path)
					}
				}
				if jobs, err := a.Jobs(ctxFor(t), scope, core.JobFilter{}); err != nil || len(jobs) != 0 {
					t.Fatalf("jobs = %v, %v; a refused submission queues nothing", jobs, err)
				}
			})
		}
	}
}

// The base itself, a command job on a changed commit, and any commit while the run has no result
// branch are not held to the base's task.
func TestOnlyCheckAndMeasureOfARunWithABranchAreHeldToTheBasesTask(t *testing.T) {
	repo := string(call.Scope.Repository())
	changed := core.Commit(strings.Repeat("1", 40))
	edited := tasktest.Scripted("task", passes, scores)
	edited[taskDir+"check.py"] = task.File{Data: []byte("changed")}
	setup := func(t *testing.T, branch bool) *app.App {
		w := newWorld(t)
		w.branches = gitrepo.NewFakeBranches()
		if branch {
			w.branches.AddBranch(repo, "vibesys/result/task", commit)
		}
		w.tasks.Put(repo, commit, tasktest.Scripted("task", passes, scores))
		w.tasks.Put(repo, changed, edited)
		return w.newApp()
	}
	t.Run("the base", func(t *testing.T) {
		_, err := setup(t, true).Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: flowSpec(core.CheckSettings{})})
		if err != nil {
			t.Fatal(err)
		}
	})
	t.Run("a command job", func(t *testing.T) {
		_, err := setup(t, true).Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: core.JobSpec{Commit: changed, Environment: core.EnvironmentHost, Settings: core.CommandSettings{Image: core.ImageHarness, Argv: []string{"exit", "0"}}}})
		if err != nil {
			t.Fatal(err)
		}
	})
	t.Run("no result branch yet", func(t *testing.T) {
		_, err := setup(t, false).Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: core.JobSpec{Commit: changed, Settings: core.CheckSettings{}}})
		if err != nil {
			t.Fatal(err)
		}
	})
}
