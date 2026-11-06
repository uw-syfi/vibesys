package gitwork_test

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
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
		if f.IsLink() {
			if err := os.Symlink(filepath.FromSlash(f.Link), full); err != nil {
				t.Fatal(err)
			}
			continue
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
	files[".vibesys/tasks/t/inside"] = task.File{Link: "check.sh"}
	files[".vibesys/tasks/t/outside"] = task.File{Link: "/tmp/made-when-the-task-runs"}
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
	// A task with no image runs here, so every link is in the checkout as the commit holds it.
	for _, rel := range []string{"inside", "outside"} {
		p := ".vibesys/tasks/t/" + rel
		if target, err := os.Readlink(filepath.Join(co.Dir, p)); err != nil || target != files[p].Link {
			t.Errorf("the checkout's %s links to %q (%v), want %q", p, target, err, files[p].Link)
		}
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

// A checkout is made whichever file system the repository and the checkouts are on. A clone that
// forces hard links from the repository's objects fails when they are on different ones, which is
// what happens when TMPDIR is on a disk of its own.
func TestACheckoutIsMadeWhenTheRepositoryIsOnAnotherFileSystem(t *testing.T) {
	repo, commit := newRepo(t, tasktest.Files("t"))
	base := onAnotherFileSystem(t, repo)
	co, remove, err := gitwork.Checkouts{Base: base}.Checkout(context.Background(), repo, commit)
	if err != nil {
		t.Fatal(err)
	}
	defer remove()
	if head := gitIn(t, co.Dir, "rev-parse", "HEAD"); head != string(commit) {
		t.Errorf("HEAD = %s, want %s", head, commit)
	}
	if _, err := os.Stat(filepath.Join(co.Dir, "src/main.c")); err != nil {
		t.Errorf("the checkout lacks a committed file: %v", err)
	}
}

// onAnotherFileSystem returns a new directory on a different file system than dir is on. The test
// skips on a machine where every place it may write is on one file system, because it has nothing
// to say there.
func onAnotherFileSystem(t *testing.T, dir string) string {
	t.Helper()
	// The device number is a different integer type on different systems, so it is compared as text.
	device := func(path string) (string, bool) {
		var st syscall.Stat_t
		if err := syscall.Stat(path, &st); err != nil {
			return "", false
		}
		return fmt.Sprint(st.Dev), true
	}
	here, ok := device(dir)
	if !ok {
		t.Fatalf("cannot stat %s", dir)
	}
	for _, candidate := range []string{"/dev/shm", "/tmp", "/var/tmp", "/run", "/mnt"} {
		if there, ok := device(candidate); !ok || there == here {
			continue
		}
		made, err := os.MkdirTemp(candidate, "vibesys-gitwork-test-")
		if err != nil {
			continue
		}
		t.Cleanup(func() { _ = os.RemoveAll(made) })
		return made
	}
	t.Skip("every directory this test may write to is on one file system")
	return ""
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

// A switch replaces a checkout in place with a clean checkout of another commit, one made after
// the checkout and on no branch included: the directory stays, the new commit's files are there,
// and nothing the commands left in it before is.
func TestASwitchReplacesACheckoutInPlace(t *testing.T) {
	repo, first := newRepo(t, task.Tree{"a.txt": {Data: []byte("one")}})
	co, remove, err := (gitwork.Checkouts{Base: t.TempDir()}).Checkout(context.Background(), repo, first)
	if err != nil {
		t.Fatal(err)
	}
	defer remove()
	// A commit made after the checkout, on no branch: what an agent's detached HEAD leaves.
	if err := os.WriteFile(filepath.Join(repo, "a.txt"), []byte("two"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitIn(t, repo, "add", "-A")
	tree := gitIn(t, repo, "write-tree")
	second := core.Commit(gitIn(t, repo, "commit-tree", tree, "-p", string(first), "-m", "second"))
	for _, left := range []string{"build/out.o", "untracked.txt"} {
		p := filepath.Join(co.Dir, left)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte("left"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	checkouts := gitwork.Checkouts{}
	for range 2 { // a second switch to the same commit changes nothing
		if err := checkouts.Switch(context.Background(), repo, co, second); err != nil {
			t.Fatal(err)
		}
	}
	if data, err := os.ReadFile(filepath.Join(co.Dir, "a.txt")); err != nil || string(data) != "two" {
		t.Fatalf("a.txt is %q, %v after the switch", data, err)
	}
	for _, left := range []string{"build", "untracked.txt"} {
		if _, err := os.Stat(filepath.Join(co.Dir, left)); !errors.Is(err, os.ErrNotExist) {
			t.Errorf("%s survived the switch: %v", left, err)
		}
	}
	if got := gitIn(t, co.Dir, "rev-parse", "HEAD"); got != string(second) {
		t.Fatalf("the checkout is at %s, want %s", got, second)
	}
	missing := core.Commit(strings.Repeat("f", 40))
	var f *core.Failure
	if err := checkouts.Switch(context.Background(), repo, co, missing); !errors.As(err, &f) || f.Kind != core.FailureNotFound {
		t.Fatalf("a switch to a commit the repository lacks: %v", err)
	}
}
