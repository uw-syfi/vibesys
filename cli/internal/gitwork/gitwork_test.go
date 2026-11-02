package gitwork_test

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/gitwork"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

func gitIn(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-C", dir, "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"}, args...)...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

// newRepo makes a repository whose one commit has the files.
func newRepo(t *testing.T, files task.Tree) (string, core.Commit) {
	t.Helper()
	dir, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	gitIn(t, dir, "init", "-q", "-b", "main")
	for p, f := range files {
		full := filepath.Join(dir, filepath.FromSlash(p))
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		mode := os.FileMode(0o644)
		if f.Executable {
			mode = 0o755
		}
		if err := os.WriteFile(full, f.Data, mode); err != nil {
			t.Fatal(err)
		}
	}
	gitIn(t, dir, "add", "-A")
	gitIn(t, dir, "commit", "-q", "--allow-empty", "-m", "first")
	return dir, core.Commit(gitIn(t, dir, "rev-parse", "HEAD"))
}

func TestTasksPassTheSourceContract(t *testing.T) {
	tasktest.Run(t, tasktest.Rig{
		Make: func(t *testing.T, files task.Tree) (task.Source, string, core.Commit) {
			repo, commit := newRepo(t, files)
			return gitwork.Tasks{}, repo, commit
		},
		Missing: core.Commit(strings.Repeat("f", 40)),
	})
}

// The task of a commit is the task as committed: edits and untracked files in the working tree, and
// a later commit, change nothing about it.
func TestTasksReadTheCommitAndNotTheWorkingTree(t *testing.T) {
	repo, commit := newRepo(t, tasktest.Files("t"))
	ctx := context.Background()
	before, err := gitwork.Tasks{}.Task(ctx, repo, commit, "t")
	if err != nil {
		t.Fatal(err)
	}
	check := filepath.Join(repo, ".vibesys/tasks/t/check.sh")
	if err := os.WriteFile(check, []byte("#!/bin/sh\nexit 7\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, ".vibesys/tasks/t/untracked"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got, err := (gitwork.Tasks{}).Task(ctx, repo, commit, "t"); err != nil || got.Version != before.Version {
		t.Fatalf("a dirty working tree changed the task: %v, %v", got.Version, err)
	}
	gitIn(t, repo, "commit", "-q", "-a", "-m", "edit")
	later := core.Commit(gitIn(t, repo, "rev-parse", "HEAD"))
	if got, err := (gitwork.Tasks{}).Task(ctx, repo, later, "t"); err != nil || got.Version == before.Version {
		t.Fatalf("the edited commit has the same task version: %v", err)
	}
	if got, err := (gitwork.Tasks{}).Task(ctx, repo, commit, "t"); err != nil || got.Version != before.Version {
		t.Fatalf("the first commit's task changed: %v", err)
	}
}

func TestTasksRefuseARepositoryGitCannotRead(t *testing.T) {
	var f *core.Failure
	_, err := gitwork.Tasks{}.Task(context.Background(), t.TempDir(), core.Commit(strings.Repeat("a", 40)), "t")
	if !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
		t.Fatalf("err = %v, want unavailable", err)
	}
}

func TestACheckoutHasTheCommittedFilesAndNothingElse(t *testing.T) {
	files := tasktest.Files("t")
	repo, commit := newRepo(t, files)
	if err := os.WriteFile(filepath.Join(repo, "src/main.c"), []byte("dirty"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, "untracked.txt"), []byte("u"), 0o644); err != nil {
		t.Fatal(err)
	}
	base, _ := filepath.EvalSymlinks(t.TempDir())
	co, remove, err := gitwork.Checkouts{Base: base}.Checkout(context.Background(), repo, commit)
	if err != nil {
		t.Fatal(err)
	}
	if co.Commit != commit || !filepath.IsAbs(co.Dir) || !strings.HasPrefix(co.Dir, base) {
		t.Fatalf("checkout = %+v", co)
	}
	got, err := os.ReadFile(filepath.Join(co.Dir, "src/main.c"))
	if err != nil || string(got) != string(files["src/main.c"].Data) {
		t.Errorf("the checkout has %q, %v; want the committed file", got, err)
	}
	if _, err := os.Stat(filepath.Join(co.Dir, "untracked.txt")); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("an untracked file is in the checkout: %v", err)
	}
	info, err := os.Stat(filepath.Join(co.Dir, ".vibesys/tasks/t/check.sh"))
	if err != nil || info.Mode()&0o111 == 0 {
		t.Errorf("the executable bit was lost: %v, %v", info, err)
	}
	if head := gitIn(t, co.Dir, "rev-parse", "HEAD"); head != string(commit) {
		t.Errorf("HEAD = %s, want %s", head, commit)
	}
	remove()
	remove() // idempotent
	if entries, _ := os.ReadDir(base); len(entries) != 0 {
		t.Errorf("a removed checkout left %v", entries)
	}
	// The repository is as it was: its work tree is still dirty in the same two ways, and it has no
	// worktree entry for the checkout.
	if status := gitIn(t, repo, "status", "--porcelain"); !strings.Contains(status, "main.c") || !strings.Contains(status, "untracked.txt") {
		t.Errorf("status = %q", status)
	}
	if list := gitIn(t, repo, "worktree", "list", "--porcelain"); strings.Count(list, "worktree ") != 1 {
		t.Errorf("worktrees = %q", list)
	}
}

func TestACheckoutOfACommitNoBranchReaches(t *testing.T) {
	repo, first := newRepo(t, tasktest.Files("t"))
	gitIn(t, repo, "checkout", "-q", "--detach", string(first))
	if err := os.WriteFile(filepath.Join(repo, "extra.txt"), []byte("e"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitIn(t, repo, "add", "-A")
	gitIn(t, repo, "commit", "-q", "-m", "loose")
	loose := core.Commit(gitIn(t, repo, "rev-parse", "HEAD"))
	// The commit was made on a detached HEAD, so main still points at the first commit; moving off it
	// leaves the commit named by no branch and no tag.
	gitIn(t, repo, "checkout", "-q", "main")
	co, remove, err := gitwork.Checkouts{Base: t.TempDir()}.Checkout(context.Background(), repo, loose)
	if err != nil {
		t.Fatal(err)
	}
	defer remove()
	if _, err := os.Stat(filepath.Join(co.Dir, "extra.txt")); err != nil {
		t.Errorf("the checkout of a commit no branch reaches lacks its file: %v", err)
	}
}

func TestACheckoutFailsAsTheInterfaceSays(t *testing.T) {
	repo, _ := newRepo(t, tasktest.Files("t"))
	base := t.TempDir()
	cases := map[string]struct {
		repo   string
		commit core.Commit
		want   core.FailureKind
	}{
		"a commit the repository lacks": {repo, core.Commit(strings.Repeat("f", 40)), core.FailureNotFound},
		"a repository git cannot read":  {t.TempDir(), core.Commit(strings.Repeat("a", 40)), core.FailureUnavailable},
		"a commit that is not a commit": {repo, core.Commit("main"), core.FailureInvalidInput},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			_, _, err := gitwork.Checkouts{Base: base}.Checkout(context.Background(), c.repo, c.commit)
			var f *core.Failure
			if !errors.As(err, &f) || f.Kind != c.want {
				t.Fatalf("err = %v, want %s", err, c.want)
			}
			if entries, _ := os.ReadDir(base); len(entries) != 0 {
				t.Errorf("a failed checkout left %v", entries)
			}
		})
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, _, err := (gitwork.Checkouts{Base: base}).Checkout(ctx, repo, core.Commit(strings.Repeat("a", 40))); err == nil {
		t.Error("a cancelled context made a checkout")
	}
	if entries, _ := os.ReadDir(base); len(entries) != 0 {
		t.Errorf("left %v", entries)
	}
}
