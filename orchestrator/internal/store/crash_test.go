package store_test

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"syscall"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store/storetest"
)

// The crash tests commit a seeded workload and die at one boundary of one commit, then reopen the
// file and require the store to be a prefix of what was committed: every result before the dying
// one, whole, and the dying one entirely present or entirely absent, as its boundary says. A kill
// inside the transaction leaves nothing of the result; a kill after it leaves all of it.

const (
	crashDir   = "ORCHESTRATOR_CRASH_DIR"
	crashSeed  = "ORCHESTRATOR_CRASH_SEED"
	crashAt    = "ORCHESTRATOR_CRASH_COMMIT"
	crashPoint = "ORCHESTRATOR_CRASH_POINT"
)

// TestCrashChild is the process that dies. It does nothing unless a parent started it.
func TestCrashChild(t *testing.T) {
	dirPath := os.Getenv(crashDir)
	if dirPath == "" {
		t.Skip("started by the crash tests, not by hand")
	}
	seed, _ := strconv.ParseUint(os.Getenv(crashSeed), 10, 64)
	at, _ := strconv.Atoi(os.Getenv(crashAt))
	point := store.Point(os.Getenv(crashPoint))
	dir, err := localfs.Resolve(dirPath, localfs.Fake{})
	if err != nil {
		t.Fatal(err)
	}
	commit := 0
	s, err := store.OpenSQLite(dir, store.SQLiteOptions{Fault: func(p store.Point) error {
		if p == store.PointBegin {
			commit++
		}
		if commit == at && p == point {
			// SIGKILL: no deferred function, no flush, no rollback. The file is as the kernel has it.
			return syscall.Kill(os.Getpid(), syscall.SIGKILL)
		}
		return nil
	}})
	if err != nil {
		t.Fatal(err)
	}
	w := storetest.NewWorkload(seed)
	for range at + 5 {
		if _, err := s.Commit(w.Next()); err != nil {
			t.Fatal(err)
		}
	}
	t.Fatalf("the process was meant to die at commit %d, point %s", at, point)
}

// survivors is what a store must hold after dying at commit number at, point p: the first
// results, all of them whole.
func survivors(at int, p store.Point) int {
	if p == store.PointAfterCommit {
		return at
	}
	return at - 1
}

func TestEveryCommitBoundaryIsAPrefix(t *testing.T) {
	for _, point := range store.Points {
		for _, at := range []int{1, 2, 3, 9, 25} {
			t.Run(fmt.Sprintf("%s/commit-%d", point, at), func(t *testing.T) {
				dirPath := t.TempDir()
				cmd := exec.Command(os.Args[0], "-test.run=^TestCrashChild$")
				cmd.Env = append(os.Environ(), crashDir+"="+dirPath, crashSeed+"=7", crashAt+"="+strconv.Itoa(at), crashPoint+"="+string(point))
				out, err := cmd.CombinedOutput()
				var exit *exec.ExitError
				if !errors.As(err, &exit) || !exit.Sys().(syscall.WaitStatus).Signaled() || exit.Sys().(syscall.WaitStatus).Signal() != syscall.SIGKILL {
					t.Fatalf("the child did not die by SIGKILL: %v\n%s", err, out)
				}
				dir, err := localfs.Resolve(dirPath, localfs.Fake{})
				if err != nil {
					t.Fatal(err)
				}
				s := openSQLite(t, dir, store.SQLiteOptions{})
				requirePrefix(t, s, 7, survivors(at, point))
			})
		}
	}
}

// requirePrefix checks that the store holds exactly the first n results of a seed's workload: the
// state, the tables, the outbox, and that the next commit continues from there.
func roundTrip(t *testing.T, events []core.Event) []core.Event {
	t.Helper()
	out, err := storetest.RoundTrip(events)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func requirePrefix(t *testing.T, s *store.SQLite, seed uint64, n int) {
	t.Helper()
	w := storetest.NewWorkload(seed)
	var events []core.Event
	var effects []core.EffectID
	var last uint64
	for range n {
		r := w.Next()
		events = append(events, r.Events...)
		for _, e := range r.Effects {
			effects = append(effects, e.ID())
		}
		last += uint64(len(r.Events))
	}
	state, err := s.Recover()
	if err != nil {
		t.Fatalf("recover: %v", err)
	}
	if want := core.Replay(core.State{}, roundTrip(t, events)); !state.Equal(want) {
		t.Fatalf("the recovered state is not the state after %d results", n)
	}
	if err := s.Verify(); err != nil {
		t.Fatalf("verify after a crash: %v", err)
	}
	snap, err := s.Snapshot()
	if err != nil || snap.Sequence != last {
		t.Fatalf("snapshot ends at %d, want %d (%v)", snap.Sequence, last, err)
	}
	claimed, err := s.Claim(time.Date(2200, 1, 1, 0, 0, 0, 0, time.UTC), 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	var got []core.EffectID
	for _, c := range claimed {
		got = append(got, c.Effect.ID())
	}
	if fmt.Sprint(got) != fmt.Sprint(effects) {
		t.Fatalf("the outbox holds %d effects, want the %d of the surviving results\n got  %v\n want %v", len(got), len(effects), got, effects)
	}
	next := w.Next()
	c, err := s.Commit(next)
	if err != nil || c.First != last+1 {
		t.Fatalf("the commit after the crash: %+v %v, want it to start at %d", c, err, last+1)
	}
}

// TestAFailedCommitLeavesTheStoreAsItWas injects an error at each boundary instead of a kill. Inside
// the transaction the commit fails and nothing remains; after it the result is durable although the
// caller was told it failed, and the sequence has moved on.
func TestAFailedCommitLeavesTheStoreAsItWas(t *testing.T) {
	boom := errors.New("injected")
	for _, point := range store.Points {
		for _, at := range []int{1, 4, 11} {
			t.Run(fmt.Sprintf("%s/commit-%d", point, at), func(t *testing.T) {
				dir := localDir(t)
				commit := 0
				armed := true
				s := openSQLite(t, dir, store.SQLiteOptions{Fault: func(p store.Point) error {
					if p == store.PointBegin {
						commit++
					}
					if armed && commit == at && p == point {
						armed = false
						return boom
					}
					return nil
				}})
				w := storetest.NewWorkload(3)
				var failed bool
				for i := 1; i <= at; i++ {
					_, err := s.Commit(w.Next())
					if i == at {
						failed = errors.Is(err, boom)
					} else if err != nil {
						t.Fatal(err)
					}
				}
				if !failed {
					t.Fatal("the injected error did not fail the commit")
				}
				requirePrefix(t, s, 3, survivors(at, point))
			})
		}
	}
}
