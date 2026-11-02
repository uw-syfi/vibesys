package filestore_test

import (
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
)

const (
	ownerRecord = "state/.owner"
	lockRecord  = "state/.owner.lock"
)

var (
	daemonA = filestore.Owner{Host: "host-a", Dir: "/run/user/1", PID: 10}
	daemonB = filestore.Owner{Host: "host-b", Dir: "/run/user/1", PID: 20}
	// A daemon of the same machine under another XDG_RUNTIME_DIR, which cannot see the supervisors
	// of the first.
	daemonC = filestore.Owner{Host: "host-a", Dir: "/run/user/2", PID: 30}
)

func openAs(f fsys.FS, owner filestore.Owner) (*filestore.Store, error) {
	return filestore.Open(stateDir, filestore.WithFS(f), filestore.WithOwner(owner))
}

func mustOpenAs(t *testing.T, f fsys.FS, owner filestore.Owner) *filestore.Store {
	t.Helper()
	s, err := openAs(f, owner)
	if err != nil {
		t.Fatalf("Open as %+v: %v", owner, err)
	}
	return s
}

func submitTo(t *testing.T, s store.Store, key string) core.Job {
	t.Helper()
	out, err := s.Submit(t.Context(), core.Submission{Spec: measureSpec("c1"), Key: core.JobKey(key), At: at(1)})
	if err != nil {
		t.Fatal(err)
	}
	return out.Job
}

func start(t *testing.T, s store.Store, job core.Job) {
	t.Helper()
	if _, err := s.Transition(t.Context(), core.Transition{Job: job.ID, From: core.JobQueued, To: core.JobRunning, At: at(2), Environment: pinned()}); err != nil {
		t.Fatal(err)
	}
}

// A daemon that holds a store shuts out every other, wherever it runs and whatever it is called,
// and the refusal says which store and who has it.
//
// A place has one daemon, so a holder from the opener's own place can only be one on its way out,
// and only that refusal says so: everywhere else it is a person's decision and no wait helps.
func TestASecondOwnerIsRefusedWhileTheFirstLives(t *testing.T) {
	successor := filestore.Owner{Host: daemonA.Host, Dir: daemonA.Dir, PID: 40}
	for _, second := range []filestore.Owner{daemonA, successor, daemonB, daemonC} {
		t.Run(fmt.Sprintf("%+v", second), func(t *testing.T) {
			mem := fsys.NewMemFS()
			first := mustOpenAs(t, mem, daemonA)
			before := mem.Files()
			_, err := openAs(mem, second)
			wantFailure(t, "second Open", err, core.FailureConflict, stateDir, "host-a", "/run/user/1", "10")
			samePlace := second.Host == daemonA.Host && second.Dir == daemonA.Dir
			if got := errors.Is(err, store.ErrOwnerLeaving); got != samePlace {
				t.Errorf("leaving = %v, want %v: %v", got, samePlace, err)
			}
			if got := mem.Files()[ownerRecord]; string(got) != string(before[ownerRecord]) {
				t.Errorf("a refused Open wrote the record: %s", got)
			}
			submitTo(t, first, "k1")
		})
	}
}

// The lock ends with the process, however it ends, so a crash leaves nothing to clean up.
func TestAnOwnerThatDiedLeavesNothingToCleanUp(t *testing.T) {
	mem := fsys.NewMemFS()
	first := mustOpenAs(t, mem, daemonA)
	submitTo(t, first, "k1")
	for _, next := range []filestore.Owner{daemonA, daemonB, daemonC} {
		image := mem.CrashImage(false)
		s := mustOpenAs(t, image, next)
		if jobs, err := s.Jobs(t.Context(), core.JobFilter{}); err != nil || len(jobs) != 1 {
			t.Errorf("as %+v: %d jobs, %v; want 1", next, len(jobs), err)
		}
	}
}

// The work of a dead owner outlives it where it ran. A daemon anywhere else cannot see that work
// and would mark a live job lost, so it is refused until a person says that place is gone.
func TestARunningJobKeepsTheStoreWithItsPlace(t *testing.T) {
	mem := fsys.NewMemFS()
	first := mustOpenAs(t, mem, daemonA)
	start(t, first, submitTo(t, first, "k1"))
	image := mem.CrashImage(false)

	for _, other := range []filestore.Owner{daemonB, daemonC} {
		before := image.Files()[ownerRecord]
		_, err := openAs(image, other)
		wantFailure(t, "Open", err, core.FailureConflict, stateDir, ownerRecord, "host-a", "/run/user/1")
		if got := image.Files()[ownerRecord]; string(got) != string(before) {
			t.Errorf("a refused Open wrote the record: %s", got)
		}
	}

	again := mustOpenAs(t, image.CrashImage(false), filestore.Owner{Host: daemonA.Host, Dir: daemonA.Dir, PID: 99})
	if jobs, err := again.Jobs(t.Context(), core.JobFilter{States: []core.JobState{core.JobRunning}}); err != nil || len(jobs) != 1 {
		t.Errorf("the same place, restarted: %d running jobs, %v; want 1", len(jobs), err)
	}

	gone := image.CrashImage(false)
	mustRemove(t, gone, ownerRecord)
	mustOpenAs(t, gone, daemonB)
}

// Jobs that are not running tie the store to nothing: a queued job has no supervisor yet, and a
// finished one needs none.
func TestAStoreWithoutARunningJobMayMove(t *testing.T) {
	mem := fsys.NewMemFS()
	first := mustOpenAs(t, mem, daemonA)
	submitTo(t, first, "k1")
	mustOpenAs(t, mem.CrashImage(false), daemonB)
}

// A filesystem whose locks keep nobody out is refused, not used with less safety.
func TestAFilesystemWithoutLocksIsRefused(t *testing.T) {
	mem := fsys.NewMemFS()
	mem.WithoutLocks()
	_, err := openAs(mem, daemonA)
	wantFailure(t, "Open", err, core.FailureConflict, stateDir, "local_lock")
}

// Whatever takes the store from under an owner, the owner finds out at its next call, before it
// writes anything, and names who has it.
func TestAnOwnerThatLostTheStoreStopsWriting(t *testing.T) {
	losses := map[string]func(t *testing.T, mem *fsys.MemFS){
		"the lock file was deleted":  func(t *testing.T, mem *fsys.MemFS) { mustRemove(t, mem, lockRecord) },
		"the server forgot the lock": func(_ *testing.T, mem *fsys.MemFS) { mem.ForgetLocks() },
		"both":                       func(t *testing.T, mem *fsys.MemFS) { mustRemove(t, mem, lockRecord); mem.ForgetLocks() },
	}
	for what, lose := range losses {
		t.Run(what, func(t *testing.T) {
			mem := fsys.NewMemFS()
			loser := mustOpenAs(t, mem, daemonA)
			job := submitTo(t, loser, "k1")
			lose(t, mem)
			winner := mustOpenAs(t, mem, daemonB)

			files := mem.Files()
			_, err := loser.Submit(t.Context(), core.Submission{Spec: measureSpec("c2"), Key: "k2", At: at(2)})
			wantFailure(t, "Submit", err, core.FailureUnavailable, stateDir, "host-b")
			_, err = loser.Transition(t.Context(), core.Transition{Job: job.ID, From: core.JobQueued, To: core.JobRunning, At: at(2), Environment: pinned()})
			wantFailure(t, "Transition", err, core.FailureUnavailable, stateDir, "host-b")
			// The failed commit makes the store read the disk again, and that stops too.
			_, err = loser.Jobs(t.Context(), core.JobFilter{})
			wantFailure(t, "Jobs", err, core.FailureUnavailable, stateDir, "host-b")
			if got := mem.Files(); len(got) != len(files) {
				t.Errorf("the loser changed the store: %d files, was %d", len(got), len(files))
			}
			start(t, winner, job)
		})
	}
}

// The same owner opening a store it closed is a new owner, so a stale handle cannot pass.
func TestAClosedStoreIsFinished(t *testing.T) {
	mem := fsys.NewMemFS()
	closed := mustOpenAs(t, mem, daemonA)
	if err := closed.Close(); err != nil {
		t.Fatal(err)
	}
	if err := closed.Close(); err != nil {
		t.Fatalf("a second Close: %v", err)
	}
	_, err := closed.Submit(t.Context(), core.Submission{Spec: measureSpec("c2"), Key: "k2", At: at(2)})
	wantFailure(t, "Submit after Close", err, core.FailureUnavailable, stateDir)
	mustOpenAs(t, mem, daemonA)
}

// An Open that fails, at any step and in any way, holds nothing: the next Open has the store, and
// no failure of an Open costs a running job its guard.
func TestAnOpenThatFailsLeavesTheStoreToTheNext(t *testing.T) {
	seed := fsys.NewMemFS()
	s := mustOpenAs(t, seed, daemonA)
	start(t, s, submitTo(t, s, "k1"))
	baseline := seed.CrashImage(false)
	probe := baseline.CrashImage(false)
	mustOpenAs(t, probe, daemonA)
	steps := probe.Steps()
	if steps == 0 {
		t.Fatal("opening took no steps, so this test tests nothing")
	}
	for _, kind := range []fsys.FaultKind{fsys.FaultFail, fsys.FaultFailAfter, fsys.FaultCrash} {
		for at := range steps {
			t.Run(fmt.Sprintf("kind %d step %d", kind, at), func(t *testing.T) {
				mem := baseline.CrashImage(false)
				mem.SetFault(fsys.Fault{At: at, Kind: kind})
				s, err := openAs(mem, daemonA)
				if kind == fsys.FaultCrash {
					if err == nil {
						t.Fatal("Open survived a crash")
					}
					mem = mem.CrashImage(false)
				} else if err == nil {
					// A failed Remove of a temporary file is ignored, so an Open can pass.
					if err := s.Close(); err != nil {
						t.Fatal(err)
					}
				}
				// Whoever died or failed, another place still cannot have the running job.
				_, err = openAs(mem, daemonB)
				wantFailure(t, "Open as another", err, core.FailureConflict, stateDir)
				again := mustOpenAs(t, mem, daemonA)
				if jobs, err := again.Jobs(t.Context(), core.JobFilter{States: []core.JobState{core.JobRunning}}); err != nil || len(jobs) != 1 {
					t.Errorf("after the fault: %d running jobs, %v; want 1", len(jobs), err)
				}
			})
		}
	}
}

// A record that is gone stops the owner, since a person who removes it under a live daemon has
// not given it leave to go on. It is not the end of the owner: the same is seen of a disk that went
// away for a moment, and when the record is back so is the owner.
func TestARecordThatIsGoneStopsTheOwnerUntilItIsBack(t *testing.T) {
	mem := fsys.NewMemFS()
	s := mustOpenAs(t, mem, daemonA)
	data := mem.Files()[ownerRecord]
	mustRemove(t, mem, ownerRecord)
	if _, err := s.Jobs(t.Context(), core.JobFilter{}); err != nil {
		t.Fatalf("Jobs before any failed commit: %v", err)
	}
	_, err := s.Submit(t.Context(), core.Submission{Spec: measureSpec("c2"), Key: "k2", At: at(2)})
	wantFailure(t, "Submit", err, core.FailureUnavailable, stateDir, ownerRecord)
	mem.Put(ownerRecord, data)
	submitTo(t, s, "k3")
}

func TestADamagedOwnerRecordIsRefusedWithItsName(t *testing.T) {
	for what, data := range map[string]string{
		"not json":      "not json",
		"no version":    `{"host":"host-a"}`,
		"unknown field": `{"format":1,"generation":1,"host":"host-a","dir":"/d","pid":1,"surprise":true}`,
	} {
		t.Run(what, func(t *testing.T) {
			mem := populated(t)
			mem.Put(ownerRecord, []byte(data))
			_, err := openAs(mem, daemonA)
			wantFailure(t, "Open", err, core.FailureInternal, ownerRecord)
		})
	}
	t.Run("a newer format", func(t *testing.T) {
		mem := populated(t)
		mem.Put(ownerRecord, []byte(`{"format":2}`))
		_, err := openAs(mem, daemonA)
		wantFailure(t, "Open", err, core.FailureIncompatible, ownerRecord)
	})
}

func FuzzOwnerRecord(f *testing.F) {
	f.Add([]byte(`{"format":1,"generation":7,"host":"host-a","dir":"/run/user/1","pid":10}`))
	f.Add([]byte(`{"format":1,"generation":18446744073709551615}`))
	f.Add([]byte(`{"format":2}`))
	f.Add([]byte(`[]`))
	f.Fuzz(func(t *testing.T, data []byte) {
		mem := fsys.NewMemFS()
		seed := mustOpenAs(t, mem, daemonA)
		if err := seed.Close(); err != nil {
			t.Fatal(err)
		}
		mem.Put(ownerRecord, data)
		s, err := openAs(mem, daemonA)
		if err != nil {
			var failure *core.Failure
			if !errors.As(err, &failure) || !strings.Contains(failure.Message, ownerRecord) {
				t.Fatalf("a record that cannot be read was refused without its name: %v", err)
			}
			return
		}
		submitTo(t, s, "k1")
	})
}
