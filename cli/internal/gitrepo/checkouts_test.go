package gitrepo_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
)

// A promotion moves a branch that a person or a lead may have checked out. A checkout of the branch
// must move with it, files and index included, or the next commit there would revert the result.
// These tests hold that for a real repository and for the fake the daemon's tests use.

// checkoutWorld is a repository with a chain of commits c[0..3] on branch "main" and the means to
// check a branch out and to look at the checkout, whatever holds the repository.
type checkoutWorld struct {
	b          gitrepo.Branches
	repository string
	c          []core.Commit
	// branch makes the branch at a commit, checked out nowhere.
	branch func(name string, at core.Commit)
	// checkOut makes a work tree with the branch checked out and returns its path.
	checkOut func(name string) string
	// dirty gives the work tree at path a local change to a tracked file.
	dirty func(path string)
	// settled says whether the work tree at path holds exactly the files of commit and no local change.
	settled func(path string, commit core.Commit) bool
	// branchAt reads where a branch is, without the interface under test.
	branchAt func(name string) core.Commit
}

func testCheckedOutBranch(t *testing.T, newWorld func(t *testing.T) checkoutWorld) {
	ctx := context.Background()

	t.Run("a clean checkout moves with the branch", func(t *testing.T) {
		w := newWorld(t)
		w.branch("res", w.c[0])
		path := w.checkOut("res")
		if err := w.b.FastForward(ctx, w.repository, "res", w.c[0], w.c[2]); err != nil {
			t.Fatalf("FastForward: %v", err)
		}
		if got := w.branchAt("res"); got != w.c[2] {
			t.Fatalf("the branch is at %.7s, want %.7s", got, w.c[2])
		}
		if !w.settled(path, w.c[2]) {
			t.Fatal("the checkout does not hold the promoted commit's files: a commit there would revert the result")
		}
		if err := w.b.FastForward(ctx, w.repository, "res", w.c[0], w.c[2]); err != nil {
			t.Errorf("the same move again: %v; an interrupted move must be repeatable", err)
		}
	})

	t.Run("a checkout with local changes refuses and nothing moves", func(t *testing.T) {
		w := newWorld(t)
		w.branch("res", w.c[0])
		path := w.checkOut("res")
		w.dirty(path)
		errs := map[string]error{
			"CheckFastForward": w.b.CheckFastForward(ctx, w.repository, "res", w.c[2]),
			"FastForward":      w.b.FastForward(ctx, w.repository, "res", w.c[0], w.c[2]),
		}
		for what, err := range errs {
			wantKind(t, what, err, core.FailureConflict)
			if err != nil && !strings.Contains(err.Error(), path) {
				t.Errorf("%s: %q does not name the checkout %s", what, err, path)
			}
		}
		if got := w.branchAt("res"); got != w.c[0] {
			t.Fatalf("the branch is at %.7s, want it unmoved at %.7s", got, w.c[0])
		}
	})

	t.Run("a branch checked out nowhere moves", func(t *testing.T) {
		w := newWorld(t)
		w.branch("res", w.c[0])
		other := w.checkOut("main")
		w.dirty(other)
		if err := w.b.CheckFastForward(ctx, w.repository, "res", w.c[1]); err != nil {
			t.Fatalf("CheckFastForward: %v", err)
		}
		if err := w.b.FastForward(ctx, w.repository, "res", w.c[0], w.c[1]); err != nil {
			t.Fatalf("FastForward: %v", err)
		}
		if got := w.branchAt("res"); got != w.c[1] {
			t.Fatalf("the branch is at %.7s, want %.7s", got, w.c[1])
		}
	})

	t.Run("a missing branch is not found", func(t *testing.T) {
		w := newWorld(t)
		wantKind(t, "CheckFastForward", w.b.CheckFastForward(ctx, w.repository, "no-such-branch", w.c[1]), core.FailureNotFound)
	})
}

func TestFakeCheckedOutBranch(t *testing.T) {
	testCheckedOutBranch(t, func(t *testing.T) checkoutWorld {
		f := gitrepo.NewFakeBranches()
		c := func(n int) core.Commit { return core.Commit(fmt.Sprintf("%040x", n+1)) }
		w := checkoutWorld{b: f, repository: "/repo", c: []core.Commit{c(0), c(1), c(2), c(3)}}
		f.AddCommit("/repo", w.c[0])
		for i := 1; i < len(w.c); i++ {
			f.AddCommit("/repo", w.c[i], w.c[i-1])
		}
		f.AddBranch("/repo", "main", w.c[0])
		w.branch = func(name string, at core.Commit) { f.AddBranch("/repo", name, at) }
		w.checkOut = func(name string) string {
			path := "/work/" + name
			f.AddCheckout("/repo", path, name)
			return path
		}
		w.dirty = func(path string) { f.DirtyCheckout("/repo", path) }
		w.settled = func(path string, commit core.Commit) bool {
			at, dirty := f.CheckoutState("/repo", path)
			return at == commit && !dirty
		}
		w.branchAt = func(name string) core.Commit { got, _ := f.At("/repo", name); return got }
		return w
	})
}

// realRepo is a repository on disk whose main.py holds "v<i>" at commit i of branch main.
type realRepo struct {
	t    *testing.T
	root string
	c    []core.Commit
}

func newRealRepo(t *testing.T) *realRepo {
	t.Helper()
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	r := &realRepo{t: t, root: filepath.Join(root, "repo")}
	if err := os.Mkdir(r.root, 0o755); err != nil {
		t.Fatal(err)
	}
	gitIn(t, r.root, "init", "-q", "-b", "main")
	gitIn(t, r.root, "config", "gc.auto", "0")
	for i := range 4 {
		r.write(r.root, "main.py", fmt.Sprintf("v%d\n", i))
		r.write(r.root, "notes.txt", "notes\n")
		gitIn(t, r.root, "add", "-A")
		gitIn(t, r.root, "commit", "-q", "-m", fmt.Sprintf("c%d", i))
		r.c = append(r.c, core.Commit(strings.TrimSpace(gitIn(t, r.root, "rev-parse", "HEAD"))))
	}
	return r
}

func (r *realRepo) write(dir, name, content string) {
	r.t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
		r.t.Fatal(err)
	}
}

// settled reports whether the work tree holds commit's main.py and has no change to a tracked file.
func (r *realRepo) settled(path string, commit core.Commit) bool {
	r.t.Helper()
	head := strings.TrimSpace(gitIn(r.t, path, "rev-parse", "HEAD"))
	status := gitIn(r.t, path, "status", "--porcelain", "--untracked-files=no")
	want := strings.TrimSpace(gitIn(r.t, r.root, "show", string(commit)+":main.py"))
	got, err := os.ReadFile(filepath.Join(path, "main.py"))
	return err == nil && head == string(commit) && status == "" && strings.TrimSpace(string(got)) == want
}

func (r *realRepo) branchAt(name string) core.Commit {
	out, err := exec.Command("git", "-C", r.root, "rev-parse", "--verify", "--quiet", "refs/heads/"+name).Output()
	if err != nil {
		return ""
	}
	return core.Commit(strings.TrimSpace(string(out)))
}

func TestSystemCheckedOutBranch(t *testing.T) {
	testCheckedOutBranch(t, func(t *testing.T) checkoutWorld {
		r := newRealRepo(t)
		n := 0
		w := checkoutWorld{b: gitrepo.System{}, repository: r.root, c: r.c, branchAt: r.branchAt, settled: r.settled}
		w.branch = func(name string, at core.Commit) { gitIn(t, r.root, "branch", name, string(at)) }
		w.checkOut = func(name string) string {
			// The main work tree is on main; every other branch is checked out in a linked one.
			if name == "main" {
				return r.root
			}
			n++
			path := filepath.Join(filepath.Dir(r.root), fmt.Sprintf("linked-%d", n))
			gitIn(t, r.root, "worktree", "add", "-q", path, name)
			return path
		}
		w.dirty = func(path string) { r.write(path, "main.py", "edited by a person\n") }
		return w
	})
}

// The scenario the first release hit: the branch is created with git checkout -b in the person's
// own checkout, then promoted. The checkout must not show the improvement as a staged revert.
func TestPromotingTheBranchTheMainCheckoutIsOnLeavesItClean(t *testing.T) {
	r := newRealRepo(t)
	gitIn(t, r.root, "checkout", "-q", "-b", "vibesys/result/default", string(r.c[0]))
	if err := (gitrepo.System{}).FastForward(context.Background(), r.root, "vibesys/result/default", r.c[0], r.c[3]); err != nil {
		t.Fatal(err)
	}
	if status := gitIn(t, r.root, "status", "--porcelain"); status != "" {
		t.Fatalf("git status after the promotion:\n%s", status)
	}
	if !r.settled(r.root, r.c[3]) {
		t.Fatal("the checkout does not hold the promoted commit")
	}
}

// A move that stopped after the files and index reached the new commit, and before the branch did,
// is finished by asking again; it is not mistaken for local changes.
func TestAMoveThatStoppedBeforeTheBranchMovedIsFinished(t *testing.T) {
	r := newRealRepo(t)
	gitIn(t, r.root, "checkout", "-q", "-b", "res", string(r.c[0]))
	gitIn(t, r.root, "read-tree", "-m", "-u", string(r.c[0]), string(r.c[2]))
	if err := (gitrepo.System{}).FastForward(context.Background(), r.root, "res", r.c[0], r.c[2]); err != nil {
		t.Fatal(err)
	}
	if !r.settled(r.root, r.c[2]) {
		t.Fatal("the checkout is not settled at the promoted commit")
	}
}

// worktrees lists every work tree of the repository with what a person would see in it: its HEAD, its
// changes to tracked files and the diff itself.
func (r *realRepo) worktrees() map[string]string {
	out := map[string]string{}
	for _, line := range strings.Split(gitIn(r.t, r.root, "worktree", "list", "--porcelain"), "\n") {
		path, ok := strings.CutPrefix(line, "worktree ")
		if !ok {
			continue
		}
		out[path] = strings.Join([]string{
			gitIn(r.t, path, "rev-parse", "HEAD"),
			gitIn(r.t, path, "status", "--porcelain", "--untracked-files=no"),
			gitIn(r.t, path, "diff", "HEAD"),
		}, "\x00")
	}
	return out
}

// perturb leaves the checkout clean or gives it one kind of local change to a tracked file, and
// may add an untracked file, which is never a change.
func (r *realRepo) perturb(rng *rand.Rand, path string) {
	if rng.IntN(2) == 0 {
		r.write(path, "untracked.log", "output\n")
	}
	switch rng.IntN(5) {
	case 0: // clean
	case 1:
		r.write(path, "main.py", "unstaged edit\n")
	case 2:
		r.write(path, "notes.txt", "staged edit\n")
		gitIn(r.t, path, "add", "notes.txt")
	case 3:
		if err := os.Remove(filepath.Join(path, "notes.txt")); err != nil {
			r.t.Fatal(err)
		}
	case 4:
		r.write(path, "main.py", "staged then edited again\n")
		gitIn(r.t, path, "add", "main.py")
		r.write(path, "main.py", "second edit\n")
	}
}

// For any arrangement of work trees holding the result branch or other branches, clean or with local
// changes, and for a first or a later promotion, a promotion either refuses and changes nothing in
// the repository, or leaves every work tree that holds the branch exactly at the promoted commit and
// every other work tree as it was.
func TestAPromotionKeepsEveryCheckoutConsistentOrChangesNothing(t *testing.T) {
	ctx := context.Background()
	const branch = "vibesys/result/default"
	for seed := range uint64(60) {
		t.Run(fmt.Sprintf("seed %d", seed), func(t *testing.T) {
			rng := rand.New(rand.NewPCG(seed, 7))
			r := newRealRepo(t)
			b := gitrepo.System{}
			// Where the branch is created: nowhere checked out, the main checkout, or a linked one.
			var holder string
			switch rng.IntN(3) {
			case 0:
				gitIn(t, r.root, "branch", branch, string(r.c[0]))
			case 1:
				gitIn(t, r.root, "checkout", "-q", "-b", branch, string(r.c[0]))
				holder = r.root
			default:
				holder = filepath.Join(filepath.Dir(r.root), "held")
				gitIn(t, r.root, "worktree", "add", "-q", "-b", branch, holder, string(r.c[0]))
			}
			// Bystanders: a linked work tree on another branch, sometimes with a change of its own.
			if rng.IntN(2) == 0 {
				side := filepath.Join(filepath.Dir(r.root), "side")
				gitIn(t, r.root, "worktree", "add", "-q", "-b", "side", side, string(r.c[1]))
				if rng.IntN(2) == 0 {
					r.write(side, "main.py", "a person's edit\n")
				}
			}
			at := 0
			for _, to := range []int{1 + rng.IntN(2), 3} {
				if to <= at {
					continue
				}
				if holder != "" {
					r.perturb(rng, holder)
				}
				before := r.worktrees()
				checkErr := b.CheckFastForward(ctx, r.root, branch, r.c[to])
				err := b.FastForward(ctx, r.root, branch, r.c[at], r.c[to])
				if (checkErr == nil) != (err == nil) {
					t.Fatalf("CheckFastForward = %v but FastForward = %v", checkErr, err)
				}
				after := r.worktrees()
				if err != nil {
					wantKind(t, "a refused promotion", err, core.FailureConflict)
					if holder == "" || !strings.Contains(err.Error(), holder) {
						t.Fatalf("refused: %v; want a refusal naming the checkout %q", err, holder)
					}
					if r.branchAt(branch) != r.c[at] {
						t.Fatalf("a refused promotion moved the branch to %.7s", r.branchAt(branch))
					}
					for path, was := range before {
						if after[path] != was {
							t.Fatalf("a refused promotion changed %s", path)
						}
					}
					// The person is asked to discard the changes, and does.
					gitIn(t, holder, "reset", "-q", "--hard", string(r.c[at]))
					continue
				}
				if r.branchAt(branch) != r.c[to] {
					t.Fatalf("the branch is at %.7s, want %.7s", r.branchAt(branch), r.c[to])
				}
				if holder != "" && !r.settled(holder, r.c[to]) {
					t.Fatalf("the checkout %s does not hold the promoted commit and nothing else", holder)
				}
				for path, was := range before {
					if path != holder && after[path] != was {
						t.Fatalf("the promotion changed %s, which does not hold the branch", path)
					}
				}
				at = to
			}
		})
	}
}
