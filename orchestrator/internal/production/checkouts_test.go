package production

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// left is what an agent left in its working copy: any mix of these.
type left uint8

const (
	leftCommit left = 1 << iota
	leftModified
	leftStaged
	leftUntracked
	leftIgnored
	leftKinds       = 5
	leftNothingMade = 1 << 7 // the copy was never made.
)

// leave makes the working copy of agent in the way state says. Ignored files are those the
// repository's info/exclude names.
func leave(t *testing.T, s *ProcessSidecars, repository string, agent core.AgentID, state left) {
	t.Helper()
	if state&leftNothingMade != 0 {
		return
	}
	path := s.checkoutDir(agent)
	gitIn(t, repository, "worktree", "add", "--detach", path)
	if state&leftCommit != 0 {
		write(t, filepath.Join(path, "committed.txt"), "work\n")
		commitAll(t, path)
	}
	if state&leftModified != 0 {
		write(t, filepath.Join(path, ".vibesys", "tasks", "demo", "OBJECTIVE.md"), "changed\n")
	}
	if state&leftStaged != 0 {
		write(t, filepath.Join(path, "tracked.txt"), "staged\n")
		gitIn(t, path, "add", "tracked.txt")
	}
	if state&leftUntracked != 0 {
		write(t, filepath.Join(path, "build", "out.bin"), "output\n")
		write(t, filepath.Join(path, "__pycache__", "m.pyc"), "bytes\n")
	}
	if state&leftIgnored != 0 {
		write(t, filepath.Join(path, "run.log"), "log\n")
	}
}

// holdsWork is whether a copy left in this state has a change to a tracked file, which is the only
// thing the run's end must not delete.
func holdsWork(state left) bool { return state&(leftModified|leftStaged) != 0 }

// For any mix of what agents left behind, a run's end removes every working copy without a change to
// a tracked file, whatever untracked or ignored output it holds, and keeps the others, named; the
// repository is left with no new untracked file and lists only itself and the kept copies; a commit
// made in a removed copy stays in the repository; and doing it again changes nothing. This is the
// live finding: a benchmark always leaves build output, so every copy was kept and 5 stayed in the
// user's repository after the run.
func TestReleasingCheckoutsRemovesEveryCopyWithoutTrackedChangesAndKeepsTheOthers(t *testing.T) {
	for seed := range uint64(40) {
		r := rand.New(rand.NewPCG(seed, 9))
		repository := newRepository(t)
		write(t, filepath.Join(repository, "tracked.txt"), "base\n")
		write(t, filepath.Join(repository, ".git", "info", "exclude"), "*.log\n")
		commitAll(t, repository)
		s := &ProcessSidecars{Root: t.TempDir()}
		var agents []core.AgentID
		want := map[core.AgentID]left{}
		for i := range 2 + r.IntN(6) {
			agent := core.AgentID(fmt.Sprintf("agent-%d-%d", seed, i))
			state := left(r.IntN(1 << leftKinds))
			if r.IntN(8) == 0 {
				state = leftNothingMade
			}
			leave(t, s, repository, agent, state)
			agents = append(agents, agent)
			want[agent] = state
		}

		kept, err := s.Release(context.Background(), repository, "run-1", agents)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		var wantKept []core.Checkout
		for _, agent := range agents {
			_, statErr := os.Stat(s.checkoutDir(agent))
			switch {
			case holdsWork(want[agent]):
				wantKept = append(wantKept, core.Checkout{Agent: agent, Location: s.checkoutDir(agent)})
				if statErr != nil {
					t.Errorf("seed %d: the copy of %s with tracked changes (%05b) was removed", seed, agent, want[agent])
				}
			case statErr == nil:
				t.Errorf("seed %d: the copy of %s (%05b) is still there", seed, agent, want[agent])
			}
		}
		if !slices.Equal(kept, wantKept) {
			t.Fatalf("seed %d: kept %v, want %v", seed, kept, wantKept)
		}
		again, err := s.Release(context.Background(), repository, "run-1", agents)
		if err != nil || !slices.Equal(again, wantKept) {
			t.Fatalf("seed %d: a second release gave %v, %v; want the same %v", seed, again, err, wantKept)
		}
		// What git lists is the repository itself and the copies that were kept, and the repository's
		// own status shows nothing new.
		out, err := exec.Command("git", "-C", repository, "worktree", "list", "--porcelain").Output()
		if err != nil {
			t.Fatal(err)
		}
		if listed := strings.Count(string(out), "worktree "); listed != 1+len(wantKept) {
			t.Fatalf("seed %d: git lists %d worktrees, want %d:\n%s", seed, listed, 1+len(wantKept), out)
		}
		if status, err := exec.Command("git", "-C", repository, "status", "--porcelain").Output(); err != nil || len(status) != 0 {
			t.Fatalf("seed %d: the repository is not clean: %q, %v", seed, status, err)
		}
	}
}

// Work committed in a working copy is in the repository after the copy is gone: removing the copy
// takes files away and not history.
func TestACommitMadeInARemovedCheckoutIsStillInTheRepository(t *testing.T) {
	repository := newRepository(t)
	commitAll(t, repository)
	s := &ProcessSidecars{Root: t.TempDir()}
	leave(t, s, repository, "agent-1", leftCommit)
	head, err := exec.Command("git", "-C", s.checkoutDir("agent-1"), "rev-parse", "HEAD").Output()
	if err != nil {
		t.Fatal(err)
	}
	if kept, err := s.Release(context.Background(), repository, "run-1", []core.AgentID{"agent-1"}); err != nil || len(kept) != 0 {
		t.Fatalf("a committed copy was kept or failed: %v, %v", kept, err)
	}
	// The copy was detached, so the commit is on no branch: it survives only because the run holds a
	// ref to it, and a garbage collection that prunes everything unreachable must leave it.
	gitIn(t, repository, "reflog", "expire", "--expire=now", "--all")
	gitIn(t, repository, "gc", "--prune=now", "-q")
	sha := strings.TrimSpace(string(head))
	if err := exec.Command("git", "-C", repository, "cat-file", "-e", sha+"^{commit}").Run(); err != nil {
		t.Fatalf("the commit made in the removed copy is gone after gc: %v", err)
	}
	ref, err := exec.Command("git", "-C", repository, "rev-parse", CommitRefPrefix+"run-1/agent-1").Output()
	if err != nil || strings.TrimSpace(string(ref)) != sha {
		t.Fatalf("the kept ref is %q, %v; want %s", ref, err, sha)
	}
}

// An agent that made no commit leaves no ref: its HEAD is the commit a branch already holds.
func TestACheckoutWithNoCommitOfItsOwnLeavesNoRef(t *testing.T) {
	repository := newRepository(t)
	commitAll(t, repository)
	s := &ProcessSidecars{Root: t.TempDir()}
	leave(t, s, repository, "agent-1", 0)
	if kept, err := s.Release(context.Background(), repository, "run-1", []core.AgentID{"agent-1"}); err != nil || len(kept) != 0 {
		t.Fatalf("a clean copy was kept or failed: %v, %v", kept, err)
	}
	refs, err := exec.Command("git", "-C", repository, "for-each-ref", CommitRefPrefix).Output()
	if err != nil || len(refs) != 0 {
		t.Fatalf("refs were written for an agent with no commit: %q, %v", refs, err)
	}
}

// When the ref cannot be written the copy stays and is reported, since removing it would let the
// commit be pruned.
func TestACheckoutWhoseCommitCannotBeKeptStays(t *testing.T) {
	repository := newRepository(t)
	commitAll(t, repository)
	s := &ProcessSidecars{Root: t.TempDir()}
	leave(t, s, repository, "agent-1", leftCommit)
	// A file where the run's ref directory would go makes the ref unwritable.
	write(t, filepath.Join(repository, ".git", "refs", "vibesys", "runs", "run-1"), "not a directory\n")
	kept, err := s.Release(context.Background(), repository, "run-1", []core.AgentID{"agent-1"})
	if err != nil || len(kept) != 1 || kept[0].Agent != "agent-1" {
		t.Fatalf("kept %v, %v; want the copy of agent-1", kept, err)
	}
	if _, err := os.Stat(s.checkoutDir("agent-1")); err != nil {
		t.Fatalf("the copy was removed although its commit was not kept: %v", err)
	}
}

// A worktree add that lost its record to a concurrent prune is tried once more, and only that
// failure and only once.
func TestAWorktreeAddThatLostItsRecordToAPruneIsTriedOnce(t *testing.T) {
	lost := []byte("fatal: could not open '/repo/.git/worktrees/worktree/locked' for writing: No such file or directory")
	other := []byte("fatal: '/x' already exists")
	for _, c := range []struct {
		name     string
		failures [][]byte
		wantErr  bool
		wantRuns int
	}{
		{"never", nil, false, 1},
		{"once", [][]byte{lost}, false, 2},
		{"twice", [][]byte{lost, lost}, true, 2},
		{"another failure", [][]byte{other}, true, 1},
	} {
		runs := 0
		h := &ProcessHolders{Git: func(context.Context, string, ...string) ([]byte, error) {
			runs++
			if runs <= len(c.failures) {
				return c.failures[runs-1], errors.New("exit status 128")
			}
			return nil, nil
		}}
		err := h.Worktree(context.Background(), "/repo", filepath.Join(t.TempDir(), "worktree"))
		if (err != nil) != c.wantErr || runs != c.wantRuns {
			t.Errorf("%s: error %v after %d runs, want error %v after %d", c.name, err, runs, c.wantErr, c.wantRuns)
		}
	}
}
