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
	top, err := g.Toplevel(ctx, sub)
	if err != nil || top != repo {
		t.Fatalf("Toplevel = %q, %v; want %q", top, err, repo)
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
	if _, err := g.Toplevel(ctx, t.TempDir()); !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
		t.Fatalf("Toplevel outside a repository = %v, want invalid_input", err)
	}
}

func TestFakeResolvesLikeGit(t *testing.T) {
	full := core.Commit("0123456789abcdef0123456789abcdef01234567")
	g := gitrepo.NewFake()
	g.AddRepo("/work/repo", map[string]core.Commit{"HEAD": full})
	ctx := context.Background()
	if top, err := g.Toplevel(ctx, "/work/repo/sub/dir"); err != nil || top != "/work/repo" {
		t.Fatalf("Toplevel = %q, %v", top, err)
	}
	if _, err := g.Toplevel(ctx, "/elsewhere"); err == nil {
		t.Fatal("Toplevel outside every repository succeeded")
	}
	for _, rev := range []string{"HEAD", string(full), string(full)[:7]} {
		if got, err := g.Commit(ctx, "/work/repo", rev); err != nil || got != full {
			t.Errorf("Commit(%q) = %q, %v", rev, got, err)
		}
	}
	if _, err := g.Commit(ctx, "/work/repo", "nope"); err == nil {
		t.Fatal("an unknown revision resolved")
	}
}
