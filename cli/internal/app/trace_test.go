package app_test

import (
	"path"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// stateArtifacts is where the world's jobs keep their artifacts, under the repository.
const stateArtifacts = ".state/artifacts"

// withTracedTask puts a task of schema 0.2 whose [trace] runs trace into the repository the tests
// submit to, and gives the world's jobs artifacts directories in the repository, as the daemon's
// machine does.
func (w *world) withTracedTask(check, bench, trace []string) {
	tree := tasktest.Scripted("task", check, bench)
	name := task.TasksDir + "/task/" + task.ManifestName
	manifest := tree[name]
	quoted := make([]string, len(trace))
	for i, a := range trace {
		quoted[i] = strconv.Quote(a)
	}
	text := strings.Replace(string(manifest.Data), `version = "0.1"`, `version = "0.2"`, 1)
	manifest.Data = []byte(text + "\n[trace]\ncommand = [" + strings.Join(quoted, ", ") + "]\ntimeout_seconds = 120\n")
	tree[name] = manifest
	w.tasks.Put(string(call.Scope.Repository()), commit, tree)
	w.artifacts = func(repository, task string, job core.JobID) string {
		return path.Join(repository, stateArtifacts, task, string(job))
	}
}

// leaveTraceFiles puts the files a trace of the job leaves in the trace/ directory of its command,
// which is the job's first when the task has no build.
func (w *world) leaveTraceFiles(job core.JobID, names ...string) {
	for _, n := range names {
		w.files.Put(path.Join(string(call.Scope.Repository()), stateArtifacts, "task", string(job), "01-trace", "trace", n), []byte("x"))
	}
}

func traceOf(t *testing.T, r core.Result) core.TraceOutcome {
	t.Helper()
	tr, ok := r.Outcome.(core.TraceOutcome)
	if !ok {
		t.Fatalf("result %s is a %T, not a trace", r.Job, r.Outcome)
	}
	return tr
}

// A trace job runs the task's [trace] once and no check, saves where its reports are and what it
// left there, and is reused by the next trace of the commit; with no-cache it runs again.
func TestATraceJobRunsTheTasksTraceAndASucceededOneIsReused(t *testing.T) {
	w := newWorld(t)
	w.withTracedTask([]string{"print", "checking", ";", "exit", "0"}, scores, []string{"print", "tracing", ";", "exit", "0"})
	w.leaveTraceFiles("j1", "tree.txt", "README.txt", "report.txt", "critical-path.txt", "waterfall.txt")
	a := w.newApp()
	job, disposition := done(t, a, core.TraceSettings{}, false)
	if disposition != core.SubmitCreated || job.State != core.JobSucceeded || job.Key == "" || job.FailedStage != "" {
		t.Fatalf("job = %s %s", disposition, describe(job))
	}
	if got := logOf(t, a, job.ID); !strings.Contains(got, "tracing") || strings.Contains(got, "checking") {
		t.Errorf("a trace ran the check, or not its trace: %q", got)
	}
	res := w.results(a)
	if len(res) != 1 || res[0].Job != job.ID || res[0].Key != job.Key {
		t.Fatalf("results = %+v", res)
	}
	tr := traceOf(t, res[0])
	if tr.ArtifactsDir != stateArtifacts+"/task/j1/01-trace" {
		t.Errorf("artifacts = %q", tr.ArtifactsDir)
	}
	if want := []string{"README.txt", "critical-path.txt", "report.txt", "tree.txt", "waterfall.txt"}; !slices.Equal(tr.Files, want) {
		t.Errorf("files = %v, want %v", tr.Files, want)
	}

	again, disposition := done(t, a, core.TraceSettings{}, false)
	if disposition != core.SubmitReused || again.ID != job.ID {
		t.Errorf("the second trace = %s %s, want the first, reused", disposition, describe(again))
	}
	if n := w.started("print"); n != 1 {
		t.Errorf("the trace ran %d times, want 1", n)
	}
	// A trace answers no check, and a check pass answers no trace.
	check, disposition := done(t, a, core.CheckSettings{}, false)
	if disposition != core.SubmitCreated || check.State != core.JobSucceeded {
		t.Errorf("a check after a trace = %s %s", disposition, describe(check))
	}

	fresh, disposition := done(t, a, core.TraceSettings{}, true)
	if disposition != core.SubmitCreated || fresh.State != core.JobSucceeded || fresh.ID == job.ID {
		t.Errorf("a no-cache trace = %s %s", disposition, describe(fresh))
	}
	if got := traceOf(t, w.results(a)[2]); got.ArtifactsDir != stateArtifacts+"/task/"+string(fresh.ID)+"/01-trace" || len(got.Files) != 0 {
		t.Errorf("the fresh trace saved %+v", got)
	}
	// The newest trace answers the next submission.
	latest, disposition := done(t, a, core.TraceSettings{}, false)
	if disposition != core.SubmitReused || latest.ID != fresh.ID {
		t.Errorf("after a fresh trace = %s %s, want the fresh one", disposition, describe(latest))
	}
}

// A trace that does not exit 0 fails in the trace stage and saves nothing, so the next one runs.
func TestAFailingTraceFailsInTheTraceStageAndIsNotReused(t *testing.T) {
	w := newWorld(t)
	w.withTracedTask(passes, scores, []string{"print", "tracing", ";", "exit", "3"})
	a := w.newApp()
	job, _ := done(t, a, core.TraceSettings{}, false)
	if job.State != core.JobFailed || job.FailedStage != core.StageTrace || job.ExitCode == nil || *job.ExitCode != 3 {
		t.Fatalf("job = %s, stage %q, code %v", describe(job), job.FailedStage, job.ExitCode)
	}
	if res := w.results(a); len(res) != 0 {
		t.Errorf("a failed trace saved %+v", res)
	}
	again, disposition := done(t, a, core.TraceSettings{}, false)
	if disposition != core.SubmitCreated || again.ID == job.ID || again.FailedStage != core.StageTrace {
		t.Errorf("the next trace = %s %s", disposition, describe(again))
	}
}

// Two traces of one commit submitted while the first waits or runs are one job.
func TestTracesSubmittedWhileOneRunsJoinIt(t *testing.T) {
	w := newWorld(t)
	w.withTracedTask(passes, scores, []string{"hold", "trace"})
	t.Cleanup(func() { w.gates.Release("trace") })
	a := w.newApp()
	first := submitFlow(t, a, core.TraceSettings{}, false)
	second := submitFlow(t, a, core.TraceSettings{}, false)
	if first.Disposition != core.SubmitCreated || second.Disposition != core.SubmitJoined || second.Job.ID != first.Job.ID {
		t.Fatalf("submissions = %s %s, %s %s", first.Disposition, first.Job.ID, second.Disposition, second.Job.ID)
	}
	own := submitFlow(t, a, core.TraceSettings{}, true)
	if own.Disposition != core.SubmitCreated || own.Job.ID == first.Job.ID {
		t.Errorf("a no-cache trace = %s %s, want a job of its own", own.Disposition, own.Job.ID)
	}
	w.gates.Release("trace")
	awaitFinal(t, a, first.Job.ID)
	awaitFinal(t, a, own.Job.ID)
}

// A task that declares no [trace] has no trace to run: the submission is refused, naming the
// section, and queues nothing.
func TestATraceOfATaskWithoutATraceIsRefused(t *testing.T) {
	w := newWorld(t)
	w.withTask(passes, scores)
	a := w.newApp()
	_, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: flowSpec(core.TraceSettings{})})
	if f := core.AsFailure(err); f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "[trace]") {
		t.Fatalf("Submit = %v, want an invalid_input failure naming [trace]", err)
	}
	if res := w.results(a); len(res) != 0 {
		t.Errorf("results = %+v", res)
	}
	if n := len(w.env.Runs()); n != 0 {
		t.Errorf("%d commands ran", n)
	}
}

// A trace takes no --no-plan: only a measure says it tests no plan.
func TestATraceTakesAPlanButNotNoPlan(t *testing.T) {
	w := newWorld(t)
	w.withTracedTask(passes, scores, passes)
	a := w.newApp()
	_, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: flowSpec(core.TraceSettings{}), NoPlan: true})
	if f := core.AsFailure(err); f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "--no-plan") {
		t.Fatalf("Submit with --no-plan = %v", err)
	}
	if _, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: flowSpec(core.TraceSettings{})}); err != nil {
		t.Fatalf("a trace from a run that names no plan: %v", err)
	}
}
