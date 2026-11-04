package gitrepo_test

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
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
	if _, err := g.Resolve(ctx, filepath.Join(repo, "no-such-dir")); !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
		t.Fatalf("Resolve of a directory that does not exist = %v, want invalid_input", err)
	}
}

// A directory that cannot be read may be right, as on a mount that is away: that is unavailable,
// which a caller tries again, and not invalid input, which says the path is wrong.
func TestSystemSaysADirectoryItCannotReadIsUnavailable(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("permissions do not stop root")
	}
	locked := filepath.Join(t.TempDir(), "locked")
	if err := os.Mkdir(locked, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(locked, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.Chmod(locked, 0o700) })
	var f *core.Failure
	if _, err := (gitrepo.System{}).Resolve(context.Background(), filepath.Join(locked, "repo")); !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
		t.Fatalf("Resolve under a directory that cannot be read = %v, want unavailable", err)
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
	g.AddWorktree("/work/linked", "/work/repo", nil)
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

// callerCase is one revision a caller names and the commit it must resolve to.
type callerCase struct {
	cwd, revision string
	want          core.Commit
}

// checkCallerCommits holds CallerCommit, with repository naming the main checkout, to cases.
func checkCallerCommits(t *testing.T, g gitrepo.Revisions, repository string, cases []callerCase) {
	t.Helper()
	for _, c := range cases {
		got, err := gitrepo.CallerCommit(context.Background(), g, repository, c.cwd, c.revision)
		if err != nil || got != c.want {
			t.Errorf("CallerCommit(%s, cwd %s, %q) = %q, %v; want %q", repository, c.cwd, c.revision, got, err, c.want)
		}
	}
}

// A caller in a linked worktree of the repository it names reads HEAD and what is relative to it
// as that worktree has them, from any directory in it; a full commit and a branch read the same
// anywhere; and a caller outside the repository reads the repository's own checkout.
func TestSystemCallerCommitReadsHEADInTheCallersWorktree(t *testing.T) {
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
	first := core.Commit(gitIn(t, repo, "rev-parse", "HEAD")[:40])
	gitIn(t, repo, "commit", "-q", "--allow-empty", "-m", "main second")
	mainHead := core.Commit(gitIn(t, repo, "rev-parse", "HEAD")[:40])
	linked := filepath.Join(base, "linked")
	gitIn(t, repo, "worktree", "add", "-q", "-b", "feature", linked, string(first))
	gitIn(t, linked, "commit", "-q", "--allow-empty", "-m", "feature second")
	linkedHead := core.Commit(gitIn(t, linked, "rev-parse", "HEAD")[:40])
	nested := filepath.Join(linked, "deep")
	if err := os.Mkdir(nested, 0o755); err != nil {
		t.Fatal(err)
	}
	other := filepath.Join(base, "other")
	if err := os.Mkdir(other, 0o755); err != nil {
		t.Fatal(err)
	}
	gitIn(t, other, "init", "-q", "-b", "main")
	gitIn(t, other, "commit", "-q", "--allow-empty", "-m", "unrelated")
	outside := t.TempDir()

	checkCallerCommits(t, gitrepo.System{}, repo, []callerCase{
		{linked, "HEAD", linkedHead},
		{nested, "HEAD", linkedHead},
		{linked, "@", linkedHead},
		{linked, "HEAD~1", first},
		{linked, string(mainHead), mainHead},
		{linked, "main", mainHead},
		{linked, "feature", linkedHead},
		{repo, "HEAD", mainHead},
		{repo, "HEAD~1", first},
		{outside, "HEAD", mainHead},
		{other, "HEAD", mainHead},
		{"", "HEAD", mainHead},
	})
}

// The Fake reads a caller's revisions as git does, given a worktree's own HEAD.
func TestFakeCallerCommitReadsHEADInTheCallersWorktree(t *testing.T) {
	first, mainHead, linkedHead := core.Commit(strings.Repeat("1", 40)), core.Commit(strings.Repeat("2", 40)), core.Commit(strings.Repeat("3", 40))
	g := gitrepo.NewFake()
	g.AddRepo("/work/repo", map[string]core.Commit{"HEAD": mainHead, "@": mainHead, "HEAD~1": first, "main": mainHead, "feature": linkedHead})
	g.AddWorktree("/work/linked", "/work/repo", map[string]core.Commit{"HEAD": linkedHead, "@": linkedHead, "HEAD~1": first})
	g.AddRepo("/work/other", map[string]core.Commit{"HEAD": first})
	checkCallerCommits(t, g, "/work/repo", []callerCase{
		{"/work/linked", "HEAD", linkedHead},
		{"/work/linked/deep", "HEAD", linkedHead},
		{"/work/linked", "@", linkedHead},
		{"/work/linked", "HEAD~1", first},
		{"/work/linked", string(mainHead), mainHead},
		{"/work/linked", "main", mainHead},
		{"/work/linked", "feature", linkedHead},
		{"/work/repo", "HEAD", mainHead},
		{"/elsewhere", "HEAD", mainHead},
		{"/work/other", "HEAD", mainHead},
		{"", "HEAD", mainHead},
	})
}
