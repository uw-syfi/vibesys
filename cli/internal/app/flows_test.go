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
	w.tasks.Put(call.Repository, commit, tasktest.Scripted("task", check, bench))
}

func flowSpec(settings core.Settings) core.JobSpec {
	return core.JobSpec{Commit: commit, Settings: settings}
}

func submitFlow(t *testing.T, a *app.App, settings core.Settings, noCache bool) core.SubmitOutcome {
	t.Helper()
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: flowSpec(settings), NoCache: noCache})
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
	st, err := w.stores.Open(context.Background(), scope)
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
	w.tasks.Put(call.Repository, other, task.Tree{
		taskDir + task.ManifestName: {Data: []byte("version = 1\nbackends = [\"cpu\"]\n[evaluator]\nname = \"x\"\n")},
	})
	junk := core.Commit(strings.Repeat("ef", 20))
	w.tasks.Put(call.Repository, junk, task.Tree{
		taskDir + task.ManifestName: {Data: []byte("version = 1\nbackends = [\"cpu\"]\nunknown_key = 1\n")},
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
			_, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: c.spec})
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
		_, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: flowSpec(settings)})
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
			var havePass bool
			for step := 0; step < 14; step++ {
				noCache, recheck := rng.Intn(4) == 0, rng.Intn(3) == 0
				checks, benches := w.started("exit"), w.started("emit")
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
					havePass = true
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
		if _, ok := r.Outcome.(core.CheckOutcome); ok {
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
