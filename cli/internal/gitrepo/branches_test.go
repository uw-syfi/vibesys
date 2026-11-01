package gitrepo_test

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
)

// history is a repository of two lines: c1, c2, c3 in a row, and s which branches off c1. The
// branch "best" starts at c1.
type history struct {
	b          gitrepo.Branches
	repository string
	c1, c2, c3 core.Commit
	s          core.Commit
	// stranger is a well-formed commit the repository does not have.
	stranger core.Commit
	// branchAt reads where a branch is, without the interface under test.
	branchAt func(branch string) core.Commit
}

func wantKind(t *testing.T, what string, err error, kind core.FailureKind) {
	t.Helper()
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != kind || strings.TrimSpace(f.Message) == "" {
		t.Errorf("%s: error = %v, want a failure of kind %s with a message", what, err, kind)
	}
}

// Every Branches passes the same contract, so the daemon's tests that use the fake are about the
// behavior of git.
func testBranchesContract(t *testing.T, h history) {
	ctx := context.Background()
	b := h.b

	t.Run("head", func(t *testing.T) {
		if got, err := b.Head(ctx, h.repository, "best"); err != nil || got != h.c1 {
			t.Fatalf("Head = %q, %v; want %q", got, err, h.c1)
		}
		_, err := b.Head(ctx, h.repository, "no-such-branch")
		wantKind(t, "a missing branch", err, core.FailureNotFound)
	})

	t.Run("ancestry", func(t *testing.T) {
		cases := []struct {
			ancestor, descendant core.Commit
			want                 bool
		}{
			{h.c1, h.c3, true}, {h.c2, h.c3, true}, {h.c3, h.c1, false}, {h.c1, h.c1, true},
			{h.s, h.c3, false}, {h.c1, h.s, true}, {h.c2, h.s, false},
		}
		for _, tc := range cases {
			got, err := b.IsAncestor(ctx, h.repository, tc.ancestor, tc.descendant)
			if err != nil || got != tc.want {
				t.Errorf("IsAncestor(%.7s, %.7s) = %v, %v; want %v", tc.ancestor, tc.descendant, got, err, tc.want)
			}
		}
		_, err := b.IsAncestor(ctx, h.repository, h.stranger, h.c3)
		wantKind(t, "a commit the repository lacks", err, core.FailureNotFound)
	})

	t.Run("fast-forward", func(t *testing.T) {
		refused := func(what string, branch string, from, to core.Commit, kind core.FailureKind, stays core.Commit) {
			t.Helper()
			wantKind(t, what, b.FastForward(ctx, h.repository, branch, from, to), kind)
			if got := h.branchAt(branch); got != stays {
				t.Errorf("%s: the branch is at %.7s, want it unmoved at %.7s", what, got, stays)
			}
		}
		refused("a missing branch", "no-such-branch", h.c1, h.c2, core.FailureNotFound, "")
		refused("a branch that is not at from", "best", h.c2, h.c3, core.FailureConflict, h.c1)
		if err := b.FastForward(ctx, h.repository, "best", h.c1, h.c3); err != nil {
			t.Fatalf("FastForward: %v", err)
		}
		if got := h.branchAt("best"); got != h.c3 {
			t.Fatalf("the branch is at %.7s, want %.7s", got, h.c3)
		}
		if err := b.FastForward(ctx, h.repository, "best", h.c1, h.c3); err != nil {
			t.Errorf("moving to where the branch already is: %v; an interrupted move must be repeatable", err)
		}
		refused("a move backwards", "best", h.c3, h.c2, core.FailureConflict, h.c3)
		refused("a side line", "best", h.c3, h.s, core.FailureConflict, h.c3)
	})
}

func TestFakeBranchesPassTheContract(t *testing.T) {
	f := gitrepo.NewFakeBranches()
	c := func(n int) core.Commit { return core.Commit(fmt.Sprintf("%040x", n)) }
	h := history{b: f, repository: "/repo", c1: c(1), c2: c(2), c3: c(3), s: c(4), stranger: c(99)}
	f.AddCommit("/repo", h.c1)
	f.AddCommit("/repo", h.c2, h.c1)
	f.AddCommit("/repo", h.c3, h.c2)
	f.AddCommit("/repo", h.s, h.c1)
	f.AddBranch("/repo", "best", h.c1)
	h.branchAt = func(branch string) core.Commit { got, _ := f.At("/repo", branch); return got }
	testBranchesContract(t, h)
}

func TestSystemBranchesPassTheContract(t *testing.T) {
	repo, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	gitIn(t, repo, "init", "-q", "-b", "main")
	commit := func(msg string) core.Commit {
		gitIn(t, repo, "commit", "-q", "--allow-empty", "-m", msg)
		return core.Commit(strings.TrimSpace(gitIn(t, repo, "rev-parse", "HEAD")))
	}
	h := history{b: gitrepo.System{}, repository: repo, stranger: core.Commit(strings.Repeat("ab", 20))}
	h.c1 = commit("c1")
	h.c2 = commit("c2")
	h.c3 = commit("c3")
	gitIn(t, repo, "checkout", "-q", "-b", "side", string(h.c1))
	h.s = commit("side")
	gitIn(t, repo, "checkout", "-q", "main")
	gitIn(t, repo, "branch", "best", string(h.c1))
	h.branchAt = func(branch string) core.Commit {
		out, err := exec.Command("git", "-C", repo, "rev-parse", "--verify", "--quiet", "refs/heads/"+branch).Output()
		if err != nil {
			return ""
		}
		return core.Commit(strings.TrimSpace(string(out)))
	}
	testBranchesContract(t, h)
}

// FailNextFastForward stands for a move that did not happen: the branch stays and the next move
// goes through.
func TestFakeBranchesCanFailAMove(t *testing.T) {
	f := gitrepo.NewFakeBranches()
	a, b := core.Commit(strings.Repeat("a", 40)), core.Commit(strings.Repeat("b", 40))
	f.AddCommit("/repo", a)
	f.AddCommit("/repo", b, a)
	f.AddBranch("/repo", "best", a)
	boom := errors.New("the process died")
	f.FailNextFastForward(boom)
	if err := f.FastForward(context.Background(), "/repo", "best", a, b); !errors.Is(err, boom) {
		t.Fatalf("FastForward = %v", err)
	}
	if got, _ := f.At("/repo", "best"); got != a {
		t.Fatalf("the failed move changed the branch to %s", got)
	}
	if err := f.FastForward(context.Background(), "/repo", "best", a, b); err != nil {
		t.Fatal(err)
	}
}
