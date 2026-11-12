package app_test

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// probePause is the fake docker environment's pause between two probes of a candidate's server.
const probePause = 7 * time.Second

// probeTimers fire every pause between two probes at once and no other timeout: whether a server
// is ready decides a run, never how long it took.
type probeTimers struct{}

func (probeTimers) Timeout(_ context.Context, d time.Duration) (<-chan struct{}, func()) {
	c := make(chan struct{})
	if d == probePause {
		close(c)
	}
	return c, func() {}
}

// twoImageTask is a task of schema 0.2 whose candidate serves with the fake environment's script.
func twoImageTask(check, bench []string) task.Tree {
	quote := func(argv []string) string {
		parts := make([]string, len(argv))
		for i, a := range argv {
			parts[i] = fmt.Sprintf("%q", a)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	}
	dir := task.TasksDir + "/task/"
	manifest := fmt.Sprintf(`
version = "0.2"
backends = ["cpu"]
metric = "ops"

[candidate]
dockerfile = "serve/Dockerfile"
context = "serve"
serve = ["print", "serving on ${PORT}", ";", "ready", ";", "hold", "forever"]
ready = "/health"
startup_timeout_seconds = 60

[accuracy]
command = %s
timeout_seconds = 60

[benchmark]
command = %s
timeout_seconds = 60
result_protocol = 2

[profile]
command = ["print", "profile load", ";", "exit", "0"]
timeout_seconds = 60
`, quote(check), quote(bench))
	return task.Tree{
		dir + task.ManifestName:  {Data: []byte(manifest)},
		dir + task.ObjectiveName: {Data: []byte(tasktest.Objectives)},
		dir + "Dockerfile":       {Data: []byte("FROM busybox:1.36.1\n")},
	}
}

// protocol2Scores is a benchmark that reports ops through the evaluator stream.
var protocol2Scores = []string{"emit", `{"kind":"hello","protocol":2,"metrics":{"ops":{}}}` + "\n" + `{"kind":"result","values":{"ops":7.5}}`}

type twoImageRig struct {
	w      *world
	a      *app.App
	docker *fakeenv.Environment
	builds atomic.Int64
}

// newTwoImageRig is a daemon with a docker placement whose every build of the candidate makes a
// new image, as a Dockerfile that downloads something that changes does.
func newTwoImageRig(t *testing.T) *twoImageRig {
	w := newWorld(t)
	w.tasks.Put(string(call.Scope.Repository()), commit, twoImageTask(passes, protocol2Scores))
	r := &twoImageRig{w: w}
	r.docker = fakeenv.New(core.EnvironmentDocker, w.gates, fakeenv.WithFiles(w.files), fakeenv.WithTimers(probeTimers{}), fakeenv.WithProbePause(probePause),
		fakeenv.WithCandidates(func(b core.CandidateBuild) (core.CandidateImage, error) {
			sum := sha256.Sum256(fmt.Appendf(nil, "%s build %d", b.Dockerfile, r.builds.Add(1)))
			return core.CandidateImage{ID: "sha256:" + hex.EncodeToString(sum[:]), Tag: "vibesys-candidate:task"}, nil
		}))
	t.Cleanup(func() {
		if n := r.docker.Active(); n != 0 {
			t.Errorf("%d commands still run after the test", n)
		}
	})
	envs := &targetRecorder{Environments: fakeenv.Environments{core.EnvironmentDocker: r.docker, core.EnvironmentHost: w.env}}
	r.a = newPlacementApp(w, []core.PlacementOffer{{Name: "local"}}, envs)
	return r
}

func (r *twoImageRig) submit(t *testing.T, settings core.Settings, noCache bool) (core.Job, core.SubmitDisposition) {
	t.Helper()
	spec := core.JobSpec{Commit: commit, Environment: core.EnvironmentDocker, Settings: settings}
	out, err := r.a.Submit(ctxFor(t), app.SubmitRequest{Context: call, NoCache: noCache, NoPlan: noPlan(spec), Spec: spec})
	if err != nil {
		t.Fatal(err)
	}
	return awaitFinal(t, r.a, out.Job.ID), out.Disposition
}

// The candidate's image is recorded with each result and is not part of its key: a later build
// of the same commit that makes another image answers from the saved result.
func TestTheCandidateImageIsRecordedWithTheResultAndIsNotPartOfItsKey(t *testing.T) {
	r := newTwoImageRig(t)
	job, disposition := r.submit(t, core.MeasureSettings{Samples: 1}, false)
	if disposition != core.SubmitCreated || job.State != core.JobSucceeded {
		t.Fatalf("measure = %s %s\n%s", disposition, describe(job), logOf(t, r.a, job.ID))
	}
	res := r.w.results(r.a)
	if len(res) == 0 {
		t.Fatal("nothing saved")
	}
	first := res[len(res)-1].CandidateImage
	if !strings.HasPrefix(first, "sha256:") {
		t.Fatalf("the result records no candidate image: %+v", res)
	}
	if got := logOf(t, r.a, job.ID); !strings.Contains(got, "serving on 18080") {
		t.Fatalf("log %q lacks the server's output", got)
	}

	again, disposition := r.submit(t, core.MeasureSettings{Samples: 1}, false)
	if disposition != core.SubmitReused || again.ID != job.ID {
		t.Fatalf("the same measure = %s %s, want the first reused", disposition, describe(again))
	}
	fresh, _ := r.submit(t, core.MeasureSettings{Samples: 1}, true)
	if fresh.Key != job.Key {
		t.Fatalf("a rebuild that made another image changed the key: %s, then %s", job.Key, fresh.Key)
	}
	images := map[string]bool{}
	for _, x := range r.w.results(r.a) {
		images[x.CandidateImage] = true
	}
	if len(images) < 2 {
		t.Fatalf("the rebuilt image is not recorded: %v", images)
	}
}

// A command in the task's image of a task of two images runs beside the candidate's server, which
// starts under the wrapper, as the check and the benchmark do; it is never saved, reused or scored.
func TestAHarnessCommandRunsBesideTheWrappedServerAndIsNeverReusedNorScored(t *testing.T) {
	r := newTwoImageRig(t)
	harness := core.CommandSettings{Image: core.ImageHarness, Wrap: []string{"print", "profiler on", ";"}, Argv: []string{"print", "harness load", ";", "exit", "0"}}
	first, _ := r.submit(t, harness, false)
	if first.State != core.JobSucceeded {
		t.Fatalf("harness command = %s\n%s", describe(first), logOf(t, r.a, first.ID))
	}
	second, disposition := r.submit(t, harness, false)
	if disposition != core.SubmitCreated || second.ID == first.ID || second.State != core.JobSucceeded {
		t.Fatalf("the second harness command = %s %s, want a new run", disposition, describe(second))
	}
	for _, x := range r.w.results(r.a) {
		if _, measured := x.Outcome.(core.MeasureOutcome); measured {
			t.Fatalf("a command saved a measurement: %+v", x)
		}
	}
	log := logOf(t, r.a, second.ID)
	for _, want := range []string{"profiler on", "harness load", "serving on", "[command] image: harness"} {
		if !strings.Contains(log, want) {
			t.Fatalf("log %q lacks %q", log, want)
		}
	}
	runs := r.docker.Runs()
	last := runs[len(runs)-1]
	if last.Candidate == nil || last.Candidate.Serve == nil || !slices.Equal(last.Candidate.Serve.Argv[:3], []string{"print", "profiler on", ";"}) {
		t.Fatalf("the server ran unwrapped: %+v", last.Candidate)
	}
	if !slices.Equal(last.Argv, harness.Argv) {
		t.Fatalf("the command ran as %q, want it unwrapped as %q", last.Argv, harness.Argv)
	}
	settings := second.Spec.Settings.(core.CommandSettings)
	if settings.Image != core.ImageHarness || len(settings.Wrap) != 0 || settings.Timeout != app.DefaultCommandTimeout+time.Minute {
		t.Fatalf("the job records %+v, want the image, the wrapper applied and the server's startup added to the limit", settings)
	}
}

// A command in the candidate's image runs there, wrapped as it asked, with no server.
func TestACandidateCommandRunsInTheCandidatesImage(t *testing.T) {
	r := newTwoImageRig(t)
	job, _ := r.submit(t, core.CommandSettings{Image: core.ImageCandidate, Wrap: []string{"print", "wrapped", ";"}, Argv: []string{"print", "in the candidate", ";", "exit", "0"}}, false)
	if job.State != core.JobSucceeded {
		t.Fatalf("candidate command = %s\n%s", describe(job), logOf(t, r.a, job.ID))
	}
	runs := r.docker.Runs()
	last := runs[len(runs)-1]
	if last.Candidate == nil || last.Candidate.Serve != nil || !slices.Equal(last.Argv[:3], []string{"print", "wrapped", ";"}) {
		t.Fatalf("ran %q with candidate %+v", last.Argv, last.Candidate)
	}
	if log := logOf(t, r.a, job.ID); strings.Contains(log, "serving on") || !strings.Contains(log, "[command] image: candidate") {
		t.Fatalf("log %q", log)
	}
}

// What makes no sense is refused before anything is queued: no image, the candidate's image of a
// task that has none, and a service of its own beside the candidate's server.
func TestACommandThatCannotRunWhereItAsksIsRefused(t *testing.T) {
	for _, c := range []struct {
		name     string
		oneImage bool
		settings core.CommandSettings
		want     string
	}{
		{"no image", false, core.CommandSettings{Argv: []string{"true"}}, "harness"},
		{"candidate of a task of one image", true, core.CommandSettings{Image: core.ImageCandidate, Argv: []string{"true"}}, "no [candidate]"},
		{"a service beside the candidate's server", false, core.CommandSettings{Image: core.ImageHarness, Argv: []string{"true"}, Service: &core.ServiceSpec{Argv: []string{"serve"}}}, "no service of its own"},
	} {
		t.Run(c.name, func(t *testing.T) {
			r := newTwoImageRig(t)
			if c.oneImage {
				r.w.tasks.Put(string(call.Scope.Repository()), commit, tasktest.Scripted("task", passes, scores))
			}
			_, err := r.a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: core.JobSpec{Commit: commit, Environment: core.EnvironmentDocker, Settings: c.settings}})
			if f := core.AsFailure(err); err == nil || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, c.want) {
				t.Fatalf("got %v, want an invalid input naming %q", err, c.want)
			}
		})
	}
}

// A command in the task's image of a task of one image runs where its check and benchmark run,
// with the wrapper in front of the command and no candidate built.
func TestAHarnessCommandOfATaskOfOneImageRunsInTheTaskImage(t *testing.T) {
	r := newTwoImageRig(t)
	r.w.tasks.Put(string(call.Scope.Repository()), commit, tasktest.Scripted("task", passes, scores))
	job, _ := r.submit(t, core.CommandSettings{Image: core.ImageHarness, Wrap: []string{"print", "wrapped", ";"}, Argv: []string{"exit", "0"}}, false)
	if job.State != core.JobSucceeded {
		t.Fatalf("harness command = %s\n%s", describe(job), logOf(t, r.a, job.ID))
	}
	runs := r.docker.Runs()
	last := runs[len(runs)-1]
	if last.Candidate != nil || !slices.Equal(last.Argv, []string{"print", "wrapped", ";", "exit", "0"}) || r.builds.Load() != 0 {
		t.Fatalf("ran %q with candidate %+v after %d builds", last.Argv, last.Candidate, r.builds.Load())
	}
}

// A task of schema 0.2 without [candidate] is a task of one image: its commands run in the task
// image, no candidate image is built, and no server is started.
func TestATaskOfSchema02WithoutACandidateRunsItsCommandsInTheTaskImage(t *testing.T) {
	r := newTwoImageRig(t)
	tree := twoImageTask(passes, protocol2Scores)
	path := task.TasksDir + "/task/" + task.ManifestName
	manifest := string(tree[path].Data)
	start, end := strings.Index(manifest, "[candidate]"), strings.Index(manifest, "[accuracy]")
	tree[path] = task.File{Data: []byte(manifest[:start] + manifest[end:])}
	r.w.tasks.Put(string(call.Scope.Repository()), commit, tree)

	job, _ := r.submit(t, core.MeasureSettings{Samples: 1}, false)
	if job.State != core.JobSucceeded {
		t.Fatalf("measure = %s\n%s", describe(job), logOf(t, r.a, job.ID))
	}
	if n := r.builds.Load(); n != 0 {
		t.Fatalf("%d candidate builds for a task without a candidate", n)
	}
	runs := r.docker.Runs()
	if len(runs) == 0 {
		t.Fatal("nothing ran")
	}
	for _, run := range runs {
		if run.Candidate != nil {
			t.Fatalf("a run has a candidate: %+v", run.Candidate)
		}
	}
	if got := logOf(t, r.a, job.ID); strings.Contains(got, "serving on") {
		t.Fatalf("a server started: %q", got)
	}
	for _, x := range r.w.results(r.a) {
		if x.CandidateImage != "" {
			t.Fatalf("the result records a candidate image: %+v", x)
		}
	}
}
