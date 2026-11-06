package production

import (
	"context"
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

// worktreeState is what an agent left in its working copy.
type worktreeState int

const (
	leftClean worktreeState = iota
	leftCommitted
	leftModified
	leftUntracked
	neverMade
)

// leave makes the working copy of agent in the way state says.
func leave(t *testing.T, s *ProcessSidecars, repository string, agent core.AgentID, state worktreeState) {
	t.Helper()
	if state == neverMade {
		return
	}
	path := s.checkoutDir(agent)
	gitIn(t, repository, "worktree", "add", "--detach", path)
	switch state {
	case leftCommitted:
		write(t, filepath.Join(path, "committed.txt"), "work\n")
		commitAll(t, path)
	case leftModified:
		write(t, filepath.Join(path, ".vibesys", "tasks", "demo", "OBJECTIVE.md"), "changed\n")
	case leftUntracked:
		write(t, filepath.Join(path, "scratch.txt"), "notes\n")
	}
}

// For any mix of what agents left behind, a run's end removes exactly the working copies that hold
// nothing git lacks and keeps the others, named; a commit made in a removed copy stays in the
// repository; and doing it again changes nothing. This is the live finding: 12 worktrees stayed in
// the user's repository after the run.
func TestReleasingCheckoutsRemovesTheCleanOnesAndKeepsTheOthers(t *testing.T) {
	for seed := range uint64(12) {
		r := rand.New(rand.NewPCG(seed, 9))
		repository := newRepository(t)
		commitAll(t, repository)
		s := &ProcessSidecars{Root: t.TempDir()}
		var agents []core.AgentID
		want := map[core.AgentID]worktreeState{}
		for i := range 2 + r.IntN(5) {
			agent := core.AgentID(fmt.Sprintf("agent-%d-%d", seed, i))
			state := worktreeState(r.IntN(5))
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
			dirty := want[agent] == leftModified || want[agent] == leftUntracked
			switch {
			case dirty:
				wantKept = append(wantKept, core.Checkout{Agent: agent, Location: s.checkoutDir(agent)})
				if statErr != nil {
					t.Errorf("seed %d: the dirty copy of %s was removed", seed, agent)
				}
			case statErr == nil:
				t.Errorf("seed %d: the %v copy of %s is still there", seed, want[agent], agent)
			}
		}
		if !slices.Equal(kept, wantKept) {
			t.Fatalf("seed %d: kept %v, want %v", seed, kept, wantKept)
		}
		again, err := s.Release(context.Background(), repository, "run-1", agents)
		if err != nil || !slices.Equal(again, wantKept) {
			t.Fatalf("seed %d: a second release gave %v, %v; want the same %v", seed, again, err, wantKept)
		}
		// What git still lists is the repository itself and the copies that were kept.
		out, err := exec.Command("git", "-C", repository, "worktree", "list", "--porcelain").Output()
		if err != nil {
			t.Fatal(err)
		}
		if listed := strings.Count(string(out), "worktree "); listed != 1+len(wantKept) {
			t.Fatalf("seed %d: git lists %d worktrees, want %d:\n%s", seed, listed, 1+len(wantKept), out)
		}
	}
}

// Work committed in a working copy is in the repository after the copy is gone: removing the copy
// takes files away and not history.
func TestACommitMadeInARemovedCheckoutIsStillInTheRepository(t *testing.T) {
	repository := newRepository(t)
	commitAll(t, repository)
	s := &ProcessSidecars{Root: t.TempDir()}
	leave(t, s, repository, "agent-1", leftCommitted)
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
	leave(t, s, repository, "agent-1", leftClean)
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
	leave(t, s, repository, "agent-1", leftCommitted)
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
