package gitrepo_test

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
)

func gitIn(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-C", dir, "-c", "user.name=t", "-c", "user.email=t@example.com"}, args...)...)
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("git %v: %v", args, err)
	}
	return string(out)
}

// System answers from a real repository: the root from a subdirectory, a branch and an
// abbreviated hash as the full commit, and a revision that is not there as not found.
func TestSystemAnswersFromARealRepository(t *testing.T) {
	repo, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	gitIn(t, repo, "init", "-q", "-b", "main")
	gitIn(t, repo, "commit", "-q", "--allow-empty", "-m", "first")
	sub := filepath.Join(repo, "a", "b")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	full := core.Commit(gitIn(t, repo, "rev-parse", "HEAD")[:40])

	g := gitrepo.System{}
	ctx := context.Background()
	for _, dir := range []string{repo, sub} {
		if got, err := g.Resolve(ctx, dir); err != nil || got != core.Repository(repo) {
			t.Fatalf("Resolve(%s) = %q, %v; want %q", dir, got, err, repo)
		}
	}
	for _, rev := range []string{"HEAD", "main", string(full), string(full)[:8]} {
		got, err := g.Commit(ctx, repo, rev)
		if err != nil || got != full {
			t.Errorf("Commit(%q) = %q, %v; want %q", rev, got, err, full)
		}
	}
	_, err = g.Commit(ctx, repo, "no-such-branch")
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureNotFound {
		t.Fatalf("Commit of a missing revision = %v, want not_found", err)
	}
	_, err = g.Commit(ctx, repo, "--output=/tmp/x")
	if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
		t.Fatalf("Commit of an option = %v, want invalid_input", err)
	}
	if _, err := g.Resolve(ctx, t.TempDir()); !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
		t.Fatalf("Resolve outside a repository = %v, want invalid_input", err)
	}
	if _, err := g.Resolve(ctx, "relative/dir"); !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
		t.Fatalf("Resolve of a relative path = %v, want invalid_input", err)
	}
}

// Every linked worktree of a repository, and every directory inside one, resolves to the
// repository's main worktree, through a symbolic link too, and reads the same commits.
func TestSystemResolvesLinkedWorktreesToOneRepository(t *testing.T) {
	base, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	repo := filepath.Join(base, "main")
	if err := os.Mkdir(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	gitIn(t, repo, "init", "-q", "-b", "main")
	gitIn(t, repo, "commit", "-q", "--allow-empty", "-m", "first")
	full := core.Commit(gitIn(t, repo, "rev-parse", "HEAD")[:40])
	linked := filepath.Join(base, "linked")
	gitIn(t, repo, "worktree", "add", "-q", "-b", "other", linked)
	nested := filepath.Join(linked, "deep", "er")
	if err := os.MkdirAll(nested, 0o755); err != nil {
		t.Fatal(err)
	}
	alias := filepath.Join(base, "alias")
	if err := os.Symlink(linked, alias); err != nil {
		t.Fatal(err)
	}
	g := gitrepo.System{}
	ctx := context.Background()
	for _, dir := range []string{repo, linked, nested, alias} {
		got, err := g.Resolve(ctx, dir)
		if err != nil || got != core.Repository(repo) {
			t.Errorf("Resolve(%s) = %q, %v; want %q", dir, got, err, repo)
		}
		if c, err := g.Commit(ctx, dir, "main"); err != nil || c != full {
			t.Errorf("Commit(%s, main) = %q, %v; want %q", dir, c, err, full)
		}
	}
	bare := filepath.Join(base, "bare.git")
	gitIn(t, base, "clone", "-q", "--bare", repo, bare)
	if got, err := g.Resolve(ctx, bare); err != nil || got != core.Repository(bare) {
		t.Errorf("Resolve(bare) = %q, %v; want %q", got, err, bare)
	}
}

func TestFakeResolvesLikeGit(t *testing.T) {
	full := core.Commit("0123456789abcdef0123456789abcdef01234567")
	g := gitrepo.NewFake()
	g.AddRepo("/work/repo", map[string]core.Commit{"HEAD": full})
	ctx := context.Background()
	g.AddWorktree("/work/linked", "/work/repo")
	for _, dir := range []string{"/work/repo", "/work/repo/sub/dir", "/work/linked", "/work/linked/x"} {
		if got, err := g.Resolve(ctx, dir); err != nil || got != "/work/repo" {
			t.Fatalf("Resolve(%s) = %q, %v", dir, got, err)
		}
		for _, rev := range []string{"HEAD", string(full), string(full)[:7]} {
			if got, err := g.Commit(ctx, dir, rev); err != nil || got != full {
				t.Errorf("Commit(%s, %q) = %q, %v", dir, rev, got, err)
			}
		}
	}
	for _, dir := range []string{"/elsewhere", "/work", "work/repo"} {
		if _, err := g.Resolve(ctx, dir); err == nil {
			t.Fatalf("Resolve(%s) outside every repository succeeded", dir)
		}
	}
	if _, err := g.Commit(ctx, "/work/repo", "nope"); err == nil {
		t.Fatal("an unknown revision resolved")
	}
}
