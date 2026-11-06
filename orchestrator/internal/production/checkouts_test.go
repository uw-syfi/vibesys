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

		kept, err := s.Release(context.Background(), repository, agents)
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
		again, err := s.Release(context.Background(), repository, agents)
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
	if kept, err := s.Release(context.Background(), repository, []core.AgentID{"agent-1"}); err != nil || len(kept) != 0 {
		t.Fatalf("a committed copy was kept or failed: %v, %v", kept, err)
	}
	if err := exec.Command("git", "-C", repository, "cat-file", "-e", strings.TrimSpace(string(head))+"^{commit}").Run(); err != nil {
		t.Fatalf("the commit made in the removed copy is gone: %v", err)
	}
}
