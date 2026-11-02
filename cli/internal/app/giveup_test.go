package app_test

import (
	"context"
	"os"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/filestores"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// place is where a daemon runs: its stores on the disk the places share, as one owner, and the
// supervisors it can find.
type place struct {
	stores    *filestores.FileStores
	survivors *supervisor.Survivors
}

// newPlace is a place on the disk of the index, whose supervisors are alive.
func (w *world) newPlace(index *filestore.Index, owner filestore.Owner, alive *supervisor.Survivors) place {
	w.t.Helper()
	p := place{stores: filestores.New(index, app.Rules(5), owner), survivors: alive}
	w.t.Cleanup(func() { _ = p.stores.Close() })
	return p
}

// startAt starts a daemon at a place.
func (w *world) startAt(p place) *app.App {
	w.t.Helper()
	a := app.New(instant(app.Config{
		Clock:        w.clock,
		Stores:       p.stores,
		Environments: fakeenv.Environments{core.EnvironmentHost: w.env},
		Supervisors:  supervisor.NewInProcess(fakeenv.Environments{core.EnvironmentHost: w.env}, w.checkouts, p.survivors),
		Logf:         func(format string, args ...any) { w.t.Logf("daemon: "+format, args...) },
	}))
	w.apps = append(w.apps, a)
	if err := a.Start(context.Background()); err != nil {
		w.t.Fatalf("Start: %v", err)
	}
	return a
}

// onDisk is a store in a repository on the real disk, and the index that knows it.
func onDisk(t *testing.T) (*filestore.Index, app.Scope, core.CallContext) {
	t.Helper()
	index, err := filestore.OpenIndex(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	repo := t.TempDir()
	return index, app.Scope{Repository: repo, Task: "task"}, core.CallContext{Repository: repo, Task: "task", Run: "run-1", Harness: "h", Agent: "agent-1"}
}

func submitIn(t *testing.T, a *app.App, cc core.CallContext, script ...string) core.Job {
	t.Helper()
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: cc, Spec: command(script...)})
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	return out.Job
}

func awaitIn(t *testing.T, a *app.App, sc app.Scope, id core.JobID, want core.JobState) {
	t.Helper()
	var got core.Job
	err := a.Watch(ctxFor(t), sc, []core.JobID{id}, func(j core.Job) error {
		got = j
		if j.State == want {
			return errDone
		}
		return nil
	})
	if got.State != want {
		t.Fatalf("job %s is %s, want %s (%v)", id, got.State, want, err)
	}
}

func removeOwnerRecord(t *testing.T, sc app.Scope) {
	t.Helper()
	if err := os.Remove(filestore.OwnerRecord(sc.Repository, sc.Task)); err != nil {
		t.Fatal(err)
	}
}

// A daemon whose machine is gone for good leaves a job running in a store on a shared disk. The
// daemon of another place finds no run of it, which proves nothing, so the job holds its store until
// the user removes the owner record, as the refusal says: then it is lost, and the store's next job
// runs.
func TestAJobOfAPlaceGoneForGoodIsLostOnceTheUserRemovesTheOwnerRecord(t *testing.T) {
	w := newWorld(t)
	index, sc, cc := onDisk(t)
	gone := w.newPlace(index, filestore.Owner{Host: "host-a", Dir: "/run/one", PID: 1}, w.survivors)
	a := w.startAt(gone)
	left := submitIn(t, a, cc, "hold", "g")
	awaitIn(t, a, sc, left.ID, core.JobRunning)
	next := submitIn(t, a, cc, "print", "next")
	a.Close()
	if err := gone.stores.Close(); err != nil {
		t.Fatal(err)
	}

	elsewhere := supervisor.NewSurvivors()
	t.Cleanup(elsewhere.Close)
	here := w.newPlace(index, filestore.Owner{Host: "host-b", Dir: "/run/two", PID: 2}, elsewhere)
	removeOwnerRecord(t, sc)
	b := w.startAt(here)
	awaitIn(t, b, sc, left.ID, core.JobLost)
	awaitIn(t, b, sc, next.ID, core.JobSucceeded)
}

// A record removed by accident under a live daemon gives up nothing that daemon can still find: the
// restarted daemon finds the run, takes the job back, and the job ends as its command does.
func TestARunFoundHereIsTakenBackFromAGiveUp(t *testing.T) {
	w := newWorld(t)
	index, sc, cc := onDisk(t)
	owner := filestore.Owner{Host: "host-a", Dir: "/run/one", PID: 1}
	first := w.newPlace(index, owner, w.survivors)
	a := w.startAt(first)
	job := submitIn(t, a, cc, "hold", "g")
	awaitIn(t, a, sc, job.ID, core.JobRunning)
	a.Close()
	if err := first.stores.Close(); err != nil {
		t.Fatal(err)
	}

	removeOwnerRecord(t, sc)
	again := w.newPlace(index, owner, w.survivors)
	b := w.startAt(again)
	awaitIn(t, b, sc, job.ID, core.JobRunning)
	given, err := again.stores.GivenUp(ctxFor(t), sc)
	if err != nil {
		t.Fatal(err)
	}
	if len(given) != 0 {
		t.Fatalf("given up after the run was found here: %v", given)
	}
	w.gates.Release("g")
	awaitIn(t, b, sc, job.ID, core.JobSucceeded)
}
