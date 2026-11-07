package store_test

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs/localfstest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store/storetest"
)

// The crash tests die, or fail, at every step of every write a commit makes: each statement and
// each COMMIT, as the database driver sees them (store.Op). After each they require the store to
// be a prefix of the committed results, whole, whatever the store's own code does to group its
// writes:
//
//   - the sequence it ends at is the end of a result, so no result is half applied;
//   - the results it holds include every one whose Commit had returned, and at most one more, the
//     one that was running;
//   - the state, the tables, and the outbox are exactly those of the results it holds: an effect is
//     queued if and only if the event that caused it is, so no effect is ever lost or invented.
//
// The invariants name no step, so a change that moves the outbox writes into a second transaction,
// or splits the events from the tables, fails them at the step between the two.

// crashHangBound is how long a child may take to reach its write step before the test gives up on it as hung.
const crashHangBound = 5 * time.Minute

const (
	crashDir  = "ORCHESTRATOR_CRASH_DIR"
	crashSeed = "ORCHESTRATOR_CRASH_SEED"
	crashStep = "ORCHESTRATOR_CRASH_STEP"
	crashRuns = "ORCHESTRATOR_CRASH_COMMITS"
)

const returnedPrefix = "returned "

// atStep is the line the child prints when it has reached the write step its parent chose. It then
// stops, with the step not yet taken, and waits to be killed.
const atStep = "at the step"

// TestCrashChild is the process that dies. It does nothing unless a parent started it. It commits a
// workload and tells its parent, as it goes, how many commits have returned. At the chosen write
// step it says so and blocks, and the parent kills it from outside.
//
// The child does not kill itself. A process that sends itself SIGKILL is not stopped by the call:
// the kernel delivers the signal later, and on macOS that was sometimes after the child had gone on
// through the rest of its workload, so the "crash" landed at no step at all (about one run in ten
// of the test below). A process that blocks at the step and is killed by another cannot go on, and
// the test no longer depends on how soon a signal arrives.
func TestCrashChild(t *testing.T) {
	dirPath := os.Getenv(crashDir)
	if dirPath == "" {
		t.Skip("started by the crash tests, not by hand")
	}
	seed, _ := strconv.ParseUint(os.Getenv(crashSeed), 10, 64)
	target, _ := strconv.Atoi(os.Getenv(crashStep))
	commits, _ := strconv.Atoi(os.Getenv(crashRuns))
	dir, err := localfs.Resolve(dirPath, localfstest.Fake{})
	if err != nil {
		t.Fatal(err)
	}
	armed, step := false, 0
	s, err := store.OpenSQLite(dir, store.SQLiteOptions{Fault: func(store.Op) error {
		if !armed {
			return nil
		}
		if step++; step == target {
			fmt.Println(atStep)
			// Wait for the parent's SIGKILL: no deferred function, no flush, no rollback. The read
			// returns only if the parent has gone without killing the child, and then it ends.
			_, _ = io.Copy(io.Discard, os.Stdin)
			panic("the parent went away before it killed the child")
		}
		return nil
	}})
	if err != nil {
		t.Fatal(err)
	}
	armed = true
	w := storetest.NewWorkload(seed)
	for i := 1; i <= commits; i++ {
		if _, err := s.Commit(w.Next()); err != nil {
			t.Fatal(err)
		}
		fmt.Printf("%s%d\n", returnedPrefix, i)
	}
	t.Fatalf("the process was meant to stop at write step %d of %d commits", target, commits)
}

// killChildAtStep starts the child, waits until it has stopped at the given write step, kills it
// with SIGKILL, and returns how many commits had returned by then. The wait ends when the child
// says it is at the step or when it exits without having been there, which fails the test.
func killChildAtStep(t *testing.T, dirPath string, seed uint64, step, commits int) int {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-test.run=^TestCrashChild$")
	cmd.Env = append(os.Environ(), crashDir+"="+dirPath, crashSeed+"="+strconv.FormatUint(seed, 10),
		crashStep+"="+strconv.Itoa(step), crashRuns+"="+strconv.Itoa(commits))
	var stderr strings.Builder
	cmd.Stderr = &stderr
	stdin, err := cmd.StdinPipe() // held open: the child waits on it, and wakes if this process dies.
	if err != nil {
		t.Fatal(err)
	}
	defer stdin.Close() //nolint:errcheck // after the child is gone there is nothing to report.
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cmd.Process.Kill() }() // a no-op once the process has been waited for.
	// A child that never reaches the step would leave the loop below reading forever. Killing it ends the
	// read and fails the test as a child that ended early. The bound only guards against a hang: a child
	// reaches its step by its own writes, never by this clock, and no run is anywhere near it.
	hang := time.AfterFunc(crashHangBound, func() { _ = cmd.Process.Kill() })
	defer hang.Stop()
	var lines []string
	returned, reached := 0, false
	for sc := bufio.NewScanner(stdout); !reached && sc.Scan(); {
		line := sc.Text()
		lines = append(lines, line)
		if v, ok := strings.CutPrefix(line, returnedPrefix); ok {
			returned, _ = strconv.Atoi(v)
		}
		reached = line == atStep
	}
	if !reached {
		err := cmd.Wait()
		t.Fatalf("the child ended before write step %d: %v\n%s%s", step, err, strings.Join(lines, "\n"), stderr.String())
	}
	if err := cmd.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	var exit *exec.ExitError
	if err := cmd.Wait(); !errors.As(err, &exit) || !exit.Sys().(syscall.WaitStatus).Signaled() || exit.Sys().(syscall.WaitStatus).Signal() != syscall.SIGKILL {
		t.Fatalf("the child did not die by SIGKILL: %v\n%s", err, stderr.String())
	}
	return returned
}

// stepsPerCommit runs a seeded workload without faults and counts the write steps of each commit.
func stepsPerCommit(t *testing.T, seed uint64, commits int) []int {
	t.Helper()
	armed, current := false, 0
	counts := make([]int, commits)
	s := openSQLite(t, localDir(t), store.SQLiteOptions{Fault: func(store.Op) error {
		if armed {
			counts[current]++
		}
		return nil
	}})
	armed = true
	w := storetest.NewWorkload(seed)
	for current = range commits {
		if _, err := s.Commit(w.Next()); err != nil {
			t.Fatal(err)
		}
	}
	return counts
}

func TestEveryWriteStepOfTheFirstCommitsIsKilledByASignal(t *testing.T) {
	const seed, commits = 7, 10
	steps := stepsPerCommit(t, seed, commits)
	first := 1 // the global number of a commit's first step
	for commit, n := range steps {
		if commit+1 == 1 || commit+1 == 2 || commit+1 == 3 || commit+1 == 9 {
			for step := first; step < first+n; step++ {
				t.Run(fmt.Sprintf("commit-%d/step-%d", commit+1, step), func(t *testing.T) {
					dirPath := t.TempDir()
					returned := killChildAtStep(t, dirPath, seed, step, commits)
					dir, err := localfs.Resolve(dirPath, localfstest.Fake{})
					if err != nil {
						t.Fatal(err)
					}
					requireConsistent(t, openSQLite(t, dir, store.SQLiteOptions{}), seed, returned, returned+1)
				})
			}
		}
		first += n
	}
}

// TestEveryWriteStepLeavesAPrefix copies the database files at every write step of a seeded workload,
// which is the state a process killed there leaves, and opens each copy. It visits every step of
// every commit, where the test above spends a process on each.
func TestEveryWriteStepLeavesAPrefix(t *testing.T) {
	const seed, commits = 11, 30
	dir := localDir(t)
	type snapshot struct {
		path     string
		returned int
	}
	var snaps []snapshot
	returned, armed := 0, false
	s := openSQLite(t, dir, store.SQLiteOptions{Fault: func(store.Op) error {
		if !armed {
			return nil
		}
		copyPath := t.TempDir()
		for _, name := range []string{store.DatabaseFile, store.DatabaseFile + "-wal"} {
			b, err := os.ReadFile(filepath.Join(dir.Path(), name))
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			if err != nil {
				t.Error(err)
				continue
			}
			if err := os.WriteFile(filepath.Join(copyPath, name), b, 0o600); err != nil {
				t.Error(err)
			}
		}
		snaps = append(snaps, snapshot{copyPath, returned})
		return nil
	}})
	armed = true
	w := storetest.NewWorkload(seed)
	for range commits {
		if _, err := s.Commit(w.Next()); err != nil {
			t.Fatal(err)
		}
		returned++
	}
	if len(snaps) < commits*3 {
		t.Fatalf("only %d write steps in %d commits", len(snaps), commits)
	}
	for i, snap := range snaps {
		d, err := localfs.Resolve(snap.path, localfstest.Fake{})
		if err != nil {
			t.Fatal(err)
		}
		t.Run(fmt.Sprintf("step-%d", i+1), func(t *testing.T) {
			requireConsistent(t, openSQLite(t, d, store.SQLiteOptions{}), seed, snap.returned, snap.returned+1)
		})
	}
}

// TestAFailedWriteStepLeavesTheStoreAsItWas fails each write step of each commit with an error, in a
// store that stays up. The commit fails, the store holds a prefix of the results, and the next
// commit continues from where the store really ends. A failure at the last step, after the COMMIT,
// is a lost reply: the result is durable and the caller was told it failed.
func TestAFailedWriteStepLeavesTheStoreAsItWas(t *testing.T) {
	const seed, commits = 3, 12
	boom := errors.New("injected")
	steps := stepsPerCommit(t, seed, commits)
	for commit := 1; commit <= commits; commit++ {
		for step := 1; step <= steps[commit-1]; step++ {
			t.Run(fmt.Sprintf("commit-%d/step-%d", commit, step), func(t *testing.T) {
				current, seen, armed := 0, 0, true
				s := openSQLite(t, localDir(t), store.SQLiteOptions{Fault: func(store.Op) error {
					if current == commit {
						if seen++; armed && seen == step {
							armed = false
							return boom
						}
					}
					return nil
				}})
				w := storetest.NewWorkload(seed)
				for current = 1; current <= commit; current++ {
					_, err := s.Commit(w.Next())
					if current == commit && !errors.Is(err, boom) {
						t.Fatalf("the injected error did not fail the commit: %v", err)
					} else if current < commit && err != nil {
						t.Fatal(err)
					}
				}
				requireConsistent(t, s, seed, commit-1, commit)
			})
		}
	}
}

func roundTrip(t *testing.T, events []core.Event) []core.Event {
	t.Helper()
	out, err := storetest.RoundTrip(events)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// requireConsistent checks that the store holds exactly the first n results of a seed's workload,
// for some n between lo and hi, and returns n. It checks the state, the tables, the outbox, and
// that the next commit continues from there.
func requireConsistent(t *testing.T, s *store.SQLite, seed uint64, lo, hi int) int {
	t.Helper()
	w := storetest.NewWorkload(seed)
	results := make([]store.Result, hi+1)
	for i := range results {
		results[i] = w.Next()
	}
	snap, err := s.Snapshot()
	if err != nil {
		t.Fatal(err)
	}
	n, end := -1, uint64(0)
	for i := 0; i <= hi; i++ {
		if i > 0 {
			end += uint64(len(results[i-1].Events))
		}
		if i >= lo && end == snap.Sequence && n < 0 {
			n = i
		}
	}
	if n < 0 {
		t.Fatalf("the store ends at sequence %d, which is not the end of any of the results %d to %d: a result was applied in part or a returned one was lost", snap.Sequence, lo, hi)
	}
	var events []core.Event
	var effects []core.EffectID
	for _, r := range results[:n] {
		events = append(events, r.Events...)
		for _, e := range r.Effects {
			effects = append(effects, e.ID())
		}
	}
	state, err := s.Recover()
	if err != nil {
		t.Fatalf("recover: %v", err)
	}
	if want := core.Replay(core.State{}, roundTrip(t, events)); !state.Equal(want) {
		t.Fatalf("the recovered state is not the state after %d results", n)
	}
	if err := s.Verify(); err != nil {
		t.Fatalf("verify after %d results: %v", n, err)
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
		t.Fatalf("the outbox holds %d effects, but the %d results the log holds caused %d: an effect was lost or invented\n got  %v\n want %v",
			len(got), n, len(effects), got, effects)
	}
	c, err := s.Commit(results[n])
	if err != nil || c.First != snap.Sequence+1 {
		t.Fatalf("the commit after the crash: %+v %v, want it to start at %d", c, err, snap.Sequence+1)
	}
	return n
}
