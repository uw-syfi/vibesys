package app_test

import (
	"bytes"
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// labRig is a daemon with one docker placement, local, of two slots, whose supervisors keep
// artifacts: what a lab needs.
type labRig struct {
	w      *world
	a      *app.App
	envs   onPlacements
	offers []core.PlacementOffer
}

// artifactsOf is where the rig keeps a job's artifacts.
func artifactsOf(_, taskName string, job core.JobID) string {
	return "/artifacts/" + taskName + "/" + string(job)
}

// labArtifacts is the artifacts directory of a lab's one run.
func labArtifacts(job core.JobID) string { return artifactsOf("", "task", job) + "/01-lab" }

func newLabRig(t *testing.T, tree task.Tree, offers ...core.PlacementOffer) *labRig {
	t.Helper()
	w := newWorld(t)
	w.tasks.Put(string(call.Scope.Repository()), commit, tree)
	if len(offers) == 0 {
		offers = []core.PlacementOffer{{Name: "local"}}
	}
	w.scheduler = app.Scheduler{
		Resources: []schedule.Resource{{ID: schedule.PlacementOf("local"), Capacity: 2}, {ID: "host", Capacity: 1}},
		Demand:    schedule.PlacementDemand{Placements: map[string]schedule.Offered{"local": {}}, Host: "host"},
	}
	r := &labRig{w: w, envs: newOnPlacements(w, "local"), offers: offers}
	t.Cleanup(func() {
		if n := r.envs.docker["local"].Active(); n != 0 {
			t.Errorf("%d labs or commands still run after the test", n)
		}
	})
	r.a = r.start()
	return r
}

// start starts a daemon over the rig's world; another after the first is closed is a restart.
func (r *labRig) start() *app.App {
	w := r.w
	sups := supervisor.NewInProcess(r.envs, w.checkouts, w.survivors).WithHost(supervisor.Host{Files: w.files, Clock: w.clock, Artifacts: artifactsOf})
	a := app.New(instant(app.Config{
		Clock: w.clock, Stores: w.stores, Environments: r.envs, Supervisors: sups, Tasks: w.tasks, Files: w.tasks,
		Repositories: w.git, Placements: r.offers, Scheduler: w.scheduler,
	}))
	w.apps = append(w.apps, a)
	if err := a.Start(ctxFor(w.t)); err != nil {
		w.t.Fatal(err)
	}
	return a
}

func oneImageTask() task.Tree {
	tree := tasktest.Scripted("task", passes, []string{"mark", "benching", ";", "hold", "bench", ";", "emit", `{"ops": 7.5}`})
	tree[task.TasksDir+"/task/Dockerfile"] = task.File{Data: []byte("FROM scratch\n")}
	return tree
}

func labSpec(timeout time.Duration) core.JobSpec {
	return core.JobSpec{Commit: commit, Environment: core.EnvironmentDocker, Settings: core.CommandSettings{Lab: &core.LabSpec{}, Timeout: timeout}}
}

func (r *labRig) startLab(t *testing.T, timeout time.Duration) core.Job {
	t.Helper()
	out, err := r.a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: labSpec(timeout), Note: "explore", Plan: 0})
	if err != nil {
		t.Fatal(err)
	}
	return out.Job
}

// call makes a lab call and returns the output it streamed and its answer.
func (r *labRig) call(t *testing.T, id core.JobID, c core.LabCall) (string, core.LabReply, error) {
	t.Helper()
	var out bytes.Buffer
	var answer core.LabReply
	err := r.a.Lab(ctxFor(t), call, id, c, func(reply core.LabReply) error {
		if o, ok := reply.(core.LabOutput); ok {
			out.Write(o.Data)
			return nil
		}
		answer = reply
		return nil
	})
	return out.String(), answer, err
}

func (r *labRig) ready(t *testing.T, id core.JobID) core.LabStatus {
	t.Helper()
	_, answer, err := r.call(t, id, core.LabStatusCall{Wait: true})
	if err != nil {
		t.Fatal(err)
	}
	s, ok := answer.(core.LabStatus)
	if !ok || s.Phase != core.LabReady {
		t.Fatalf("the lab answered %+v, want it ready", answer)
	}
	return s
}

func failureKind(err error) core.FailureKind {
	var f *core.Failure
	if errors.As(err, &f) {
		return f.Kind
	}
	return ""
}

// A lab of a task of one image holds one container of the task's image, the candidate's room, for
// its time: a command run in the foreground streams its output and answers its exit code, a
// detached one is listed and ended by its handle, and its output goes to the lab's log. Ended, the
// lab succeeds as a command job, with no key and no result, so nothing of it can be compared.
func TestALabRunsWhatItIsAskedAndSavesNoResult(t *testing.T) {
	r := newLabRig(t, oneImageTask())
	lab := r.startLab(t, 0)
	if lab.Key != "" || lab.Spec.Settings.(core.CommandSettings).Timeout != core.DefaultLabTime {
		t.Fatalf("the lab was queued as %+v; want no key and the default time", lab)
	}
	s := r.ready(t, lab.ID)
	if !slices.Equal(s.Rooms, []core.LabRoom{core.LabCandidate}) || s.Commit != commit || s.Port != fakeenv.CandidatePort || s.Until.IsZero() {
		t.Fatalf("status %+v", s)
	}
	out, answer, err := r.call(t, lab.ID, core.LabExec{Room: core.LabCandidate, Argv: []string{"print", "hello", ";", "exit", "3"}, Env: map[string]string{"A": "1"}})
	if err != nil || out != "hello\n" || answer != (core.LabExit{Code: 3}) {
		t.Fatalf("exec: %q, %+v, %v", out, answer, err)
	}
	if _, _, err := r.call(t, lab.ID, core.LabExec{Room: core.LabTask, Argv: []string{"print", "x"}}); failureKind(err) != core.FailureInvalidInput {
		t.Fatalf("an exec in the task's room of a task of one image: %v", err)
	}
	_, answer, err = r.call(t, lab.ID, core.LabExec{Room: core.LabCandidate, Argv: []string{"print", "serving", ";", "hold", "server"}, Detach: true})
	p, ok := answer.(core.LabProcess)
	if err != nil || !ok || p.Handle != "p2" || p.End != nil {
		t.Fatalf("detached exec: %+v, %v", answer, err)
	}
	awaitOutput(t, r.a, lab.ID, "[p2] serving")
	_, answer, err = r.call(t, lab.ID, core.LabKillCall{Handle: p.Handle, Signal: core.LabTerm})
	if err != nil {
		t.Fatal(err)
	}
	if _, answer, _ = r.call(t, lab.ID, core.LabStatusCall{}); len(answer.(core.LabStatus).Processes) != 2 {
		t.Fatalf("ps lists %+v", answer)
	}
	if _, _, err := r.call(t, lab.ID, core.LabEndCall{}); err != nil {
		t.Fatal(err)
	}
	done := awaitFinal(t, r.a, lab.ID)
	if done.State != core.JobSucceeded || done.ResultJob != "" || done.Key != "" {
		t.Fatalf("the ended lab is %+v; want succeeded with no result", done)
	}
	if _, _, err := r.call(t, lab.ID, core.LabExec{Room: core.LabCandidate, Argv: []string{"print", "x"}}); failureKind(err) != core.FailureConflict {
		t.Fatalf("an exec on an ended lab: %v", err)
	}
	if _, answer, err := r.call(t, lab.ID, core.LabStatusCall{}); err != nil || answer.(core.LabStatus).Phase != core.LabEnded {
		t.Fatalf("the status of an ended lab: %+v, %v", answer, err)
	}
}

// A lab of a task of two images holds the candidate's container and the task's beside it, and
// builds the candidate's image from its commit first.
func TestALabOfATaskOfTwoImagesHasBothRooms(t *testing.T) {
	r := newLabRig(t, twoImageTask(passes, protocol2Scores))
	lab := r.startLab(t, time.Hour)
	s := r.ready(t, lab.ID)
	if !slices.Equal(s.Rooms, []core.LabRoom{core.LabCandidate, core.LabTask}) {
		t.Fatalf("rooms %v", s.Rooms)
	}
	if built := r.envs.docker["local"].CandidatesBuilt(); len(built) != 1 || built[0].Dockerfile != "serve/Dockerfile" {
		t.Fatalf("built %+v", built)
	}
	out, answer, err := r.call(t, lab.ID, core.LabExec{Room: core.LabTask, Argv: []string{"print", "load"}})
	if err != nil || out != "load\n" || answer != (core.LabExit{}) {
		t.Fatalf("exec in the task's room: %q %+v %v", out, answer, err)
	}
	r.end(t, lab.ID)
}

func (r *labRig) end(t *testing.T, id core.JobID) core.Job {
	t.Helper()
	if _, _, err := r.call(t, id, core.LabEndCall{}); err != nil {
		t.Fatal(err)
	}
	return awaitFinal(t, r.a, id)
}

// A lab holds a slot of its placement as a command job does, so a measure, which holds its
// placement alone, waits until the lab ends, and a lab never starts beside a running measure.
func TestAMeasureNeverSharesItsPlacementWithALab(t *testing.T) {
	r := newLabRig(t, oneImageTask())
	lab := r.startLab(t, time.Hour)
	r.ready(t, lab.ID)
	measure, err := r.a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: core.JobSpec{Commit: commit, Environment: core.EnvironmentDocker, Settings: core.MeasureSettings{Samples: 1}}})
	if err != nil {
		t.Fatal(err)
	}
	wait := r.a.Waits(scope, []core.JobID{measure.Job.ID})[measure.Job.ID]
	if wait.Kind != core.WaitInUse || len(wait.Holders) != 1 || wait.Holders[0].Job != lab.ID {
		t.Fatalf("the measure waits as %+v; want the placement in use by the lab", wait)
	}
	r.end(t, lab.ID)
	if !r.envs.gates["local"].Reached(ctxFor(t), "benching") {
		t.Fatal("the measure never started once the lab ended")
	}
	r.envs.gates["local"].Release("bench")
	awaitState(t, r.a, measure.Job.ID, core.JobSucceeded)
}

// A lab outlives a restart of the daemon: its supervisor holds it, and the next daemon adopts it,
// so a call after the restart reaches the same lab, with the processes it started.
func TestARestartedDaemonReachesTheLabItLeft(t *testing.T) {
	r := newLabRig(t, oneImageTask())
	lab := r.startLab(t, time.Hour)
	r.ready(t, lab.ID)
	_, started, err := r.call(t, lab.ID, core.LabExec{Room: core.LabCandidate, Argv: []string{"hold", "forever"}, Detach: true})
	if err != nil {
		t.Fatal(err)
	}
	r.a.Close()
	r.a = r.start()
	got := awaitState(t, r.a, lab.ID, core.JobRunning)
	if got.State != core.JobRunning {
		t.Fatalf("after the restart the lab is %s", got.State)
	}
	_, answer, err := r.call(t, lab.ID, core.LabStatusCall{})
	s, _ := answer.(core.LabStatus)
	if err != nil || s.Phase != core.LabReady || len(s.Processes) != 1 || s.Processes[0].Handle != started.(core.LabProcess).Handle || s.Processes[0].End != nil {
		t.Fatalf("after the restart: %+v, %v", answer, err)
	}
	if done := r.end(t, lab.ID); done.State != core.JobSucceeded {
		t.Fatalf("the lab ended %s", done.State)
	}
}

// A lab's time is capped by its placement's: one that asks for more than the placement runs a
// command lasts what the placement allows.
func TestALabLastsNoLongerThanItsPlacementRuns(t *testing.T) {
	r := newLabRig(t, oneImageTask(), core.PlacementOffer{Name: "local", LongestRun: 20 * time.Minute})
	lab := r.startLab(t, 3*time.Hour)
	s := r.ready(t, lab.ID)
	if got := s.Until.Sub(r.w.clock.Now()); got > 20*time.Minute || got < 19*time.Minute {
		t.Fatalf("the lab is until %v from now, want 20m", got)
	}
	r.end(t, lab.ID)
}

// A submission that is not a lab the daemon can hold is refused, naming why, and queues nothing.
func TestALabThatCannotBeHeldIsRefused(t *testing.T) {
	r := newLabRig(t, oneImageTask())
	for name, spec := range map[string]core.JobSpec{
		"host":         {Commit: commit, Environment: core.EnvironmentHost, Settings: core.CommandSettings{Lab: &core.LabSpec{}}},
		"too long":     labSpec(25 * time.Hour),
		"with command": {Commit: commit, Environment: core.EnvironmentDocker, Settings: core.CommandSettings{Lab: &core.LabSpec{}, Argv: []string{"x"}}},
	} {
		if _, err := r.a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec}); failureKind(err) != core.FailureInvalidInput {
			t.Errorf("%s: %v, want refused as invalid input", name, err)
		}
	}
	jobs, err := r.a.Jobs(ctxFor(t), scope, core.JobFilter{})
	if err != nil || len(jobs) != 0 {
		t.Fatalf("jobs %v, %v", jobs, err)
	}
	cmd, err := r.a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: core.JobSpec{Commit: commit, Environment: core.EnvironmentHost, Settings: core.CommandSettings{Argv: []string{"exit", "0"}}}})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := r.call(t, cmd.Job.ID, core.LabStatusCall{}); failureKind(err) != core.FailureInvalidInput {
		t.Fatalf("a lab call on a command job: %v", err)
	}
	awaitFinal(t, r.a, cmd.Job.ID)
}

// Ending a lab that is still queued cancels it.
func TestEndingAQueuedLabCancelsIt(t *testing.T) {
	r := newLabRig(t, oneImageTask())
	measure, err := r.a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: core.JobSpec{Commit: commit, Environment: core.EnvironmentDocker, Settings: core.MeasureSettings{Samples: 1}}})
	if err != nil {
		t.Fatal(err)
	}
	if !r.envs.gates["local"].Reached(ctxFor(t), "benching") {
		t.Fatal("the measure never benched")
	}
	lab := r.startLab(t, time.Hour)
	if _, answer, err := r.call(t, lab.ID, core.LabStatusCall{}); err != nil || answer.(core.LabStatus).Phase != core.LabQueued {
		t.Fatalf("a lab behind a measure: %+v, %v", answer, err)
	}
	if done := r.end(t, lab.ID); done.State != core.JobCancelled {
		t.Fatalf("the queued lab ended %s, want cancelled", done.State)
	}
	r.envs.gates["local"].Release("bench")
	awaitState(t, r.a, measure.Job.ID, core.JobSucceeded)
}

// Files go into the lab's artifacts directory and come back out, a directory with its files; a
// path that climbs out of it is refused.
func TestALabTakesFilesInAndGivesThemBack(t *testing.T) {
	r := newLabRig(t, oneImageTask())
	lab := r.startLab(t, time.Hour)
	r.ready(t, lab.ID)
	for _, p := range []core.LabPush{
		{Path: "in/a.sh", Data: []byte("#!/bin/sh\n"), Executable: true},
		{Path: "in/a.sh", Offset: 10, Data: []byte("echo hi\n"), Executable: true, Last: true},
		{Path: "in/b/c.txt", Data: []byte("c"), Last: true},
	} {
		if _, _, err := r.call(t, lab.ID, p); err != nil {
			t.Fatal(err)
		}
	}
	got := map[string]string{}
	err := r.a.Lab(ctxFor(t), call, lab.ID, core.LabPull{Path: "in"}, func(reply core.LabReply) error {
		if f, ok := reply.(core.LabFile); ok {
			got[f.Path] += string(f.Data)
		}
		return nil
	})
	if err != nil || got["a.sh"] != "#!/bin/sh\necho hi\n" || got["b/c.txt"] != "c" || len(got) != 2 {
		t.Fatalf("pulled %v, %v", got, err)
	}
	if data, err := r.w.files.ReadFile(labArtifacts(lab.ID)+"/in/b/c.txt", 10); err != nil || string(data) != "c" {
		t.Fatalf("the artifacts directory has %q, %v", data, err)
	}
	if _, _, err := r.call(t, lab.ID, core.LabPull{Path: "missing"}); failureKind(err) != core.FailureNotFound {
		t.Fatalf("pulling nothing: %v", err)
	}
	r.end(t, lab.ID)
}

// A lab's checkout is replaced with another commit's when the images are the lab's, while no
// process runs in it; a commit whose task differs takes another lab.
func TestALabChecksOutAnotherCommitOfTheSameImages(t *testing.T) {
	r := newLabRig(t, oneImageTask())
	repo := string(call.Scope.Repository())
	same := core.Commit(strings.Repeat("1", 40))
	other := core.Commit(strings.Repeat("2", 40))
	r.w.tasks.Put(repo, same, oneImageTask())
	changed := oneImageTask()
	changed[task.TasksDir+"/task/Dockerfile"] = task.File{Data: []byte("FROM busybox\n")}
	r.w.tasks.Put(repo, other, changed)
	lab := r.startLab(t, time.Hour)
	r.ready(t, lab.ID)
	_, answer, err := r.call(t, lab.ID, core.LabCheckoutCall{Commit: same})
	if err != nil || answer.(core.LabStatus).Commit != same || !slices.Equal(r.w.checkouts.Switched(), []core.Commit{same}) {
		t.Fatalf("checkout of the same images: %+v, %v, switched %v", answer, err, r.w.checkouts.Switched())
	}
	if _, _, err := r.call(t, lab.ID, core.LabCheckoutCall{Commit: other}); failureKind(err) != core.FailureConflict || !strings.Contains(err.Error(), "new lab") {
		t.Fatalf("checkout of another task: %v", err)
	}
	if _, _, err := r.call(t, lab.ID, core.LabExec{Room: core.LabCandidate, Argv: []string{"hold", "forever"}, Detach: true}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := r.call(t, lab.ID, core.LabCheckoutCall{Commit: commit}); failureKind(err) != core.FailureConflict || !strings.Contains(err.Error(), "p1") {
		t.Fatalf("checkout under a running process: %v", err)
	}
	r.end(t, lab.ID)
}

// A lab that ends asks what still runs in it to terminate, and waits for it within the grace,
// before its containers go: a profiler that writes what it gathered when told to stop gets to.
func TestAnEndingLabLetsItsProcessesCleanUp(t *testing.T) {
	r := newLabRig(t, oneImageTask())
	lab := r.startLab(t, time.Hour)
	r.ready(t, lab.ID)
	if _, _, err := r.call(t, lab.ID, core.LabExec{Room: core.LabCandidate, Argv: []string{"on-term", "{", "print", "flushed", "}", ";", "hold", "forever"}, Detach: true}); err != nil {
		t.Fatal(err)
	}
	r.end(t, lab.ID)
	if log := logOf(t, r.a, lab.ID); !strings.Contains(log, "[p1] flushed") || !strings.Contains(log, "[lab] p1 exited 0") {
		t.Fatalf("the lab's log %q lacks what its process wrote when told to stop", log)
	}
}

// queuedIn is the job of a queue view with the id, and whether it is there.
func queuedIn(v core.QueueView, id core.JobID) (core.QueuedJob, bool) {
	for _, g := range v.Groups {
		for _, j := range g.Jobs {
			if j.Job.ID == id {
				return j, true
			}
		}
	}
	return core.QueuedJob{}, false
}

// A lab is a queued job as any other: a draining daemon starts none, and a held lab keeps its place
// while a lab after it starts. Both are in the task's queue, waiting on the drain or the hold, and a
// call that would wait for either is refused with how it starts rather than left waiting for an
// operator; a status answers at once that it is queued. Once resumed and released, each starts.
func TestALabIsHeldAndDrainedAsAnyQueuedJob(t *testing.T) {
	r := newLabRig(t, oneImageTask())
	ctx := ctxFor(t)
	d, err := r.a.Drain(ctx, "lead", "a new build")
	if err != nil {
		t.Fatal(err)
	}
	held, after := r.startLab(t, time.Hour), r.startLab(t, time.Hour)
	if _, err := r.a.HoldJob(ctx, call, held.ID, "after the profile"); err != nil {
		t.Fatal(err)
	}
	q, err := r.a.Queue(ctx, scope)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := queuedIn(q.View, held.ID); !ok || q.Drain == nil {
		t.Fatalf("the queue %+v does not show the held lab and the drain", q)
	}
	if wait := q.Waits[after.ID]; wait.Kind != core.WaitHeld || wait.Detail != d.WaitDetail() {
		t.Fatalf("the lab waits as %+v while the daemon drains, want on the drain", wait)
	}
	for _, c := range []core.LabCall{core.LabStatusCall{Wait: true}, core.LabExec{Argv: []string{"true"}}} {
		_, _, err := r.call(t, after.ID, c)
		if f := core.AsFailure(err); f.Kind != core.FailureConflict || !strings.Contains(f.Message, d.WaitDetail()) {
			t.Fatalf("%T on a lab the daemon will not start = %v, want a conflict naming the drain", c, err)
		}
	}
	if _, answer, err := r.call(t, after.ID, core.LabStatusCall{}); err != nil || answer.(core.LabStatus).Phase != core.LabQueued {
		t.Fatalf("the status of a lab behind a drain = %+v, %v; want queued", answer, err)
	}

	if err := r.a.Resume(ctx); err != nil {
		t.Fatal(err)
	}
	r.ready(t, after.ID)
	job, err := r.a.Job(ctx, scope, held.ID)
	if err != nil || job.State != core.JobQueued || job.Hold == nil {
		t.Fatalf("the held lab is %+v, %v; want it queued and held", job, err)
	}
	if _, _, err := r.call(t, held.ID, core.LabStatusCall{Wait: true}); !strings.Contains(core.AsFailure(err).Message, job.Hold.WaitDetail()) {
		t.Fatalf("a wait for a held lab = %v, want a conflict naming the hold", err)
	}
	if _, err := r.a.ReleaseJob(ctx, call, held.ID); err != nil {
		t.Fatal(err)
	}
	r.ready(t, held.ID)
	for _, id := range []core.JobID{held.ID, after.ID} {
		if done := r.end(t, id); done.State != core.JobSucceeded {
			t.Fatalf("lab %s ended %s, want succeeded", id, done.State)
		}
	}
}
