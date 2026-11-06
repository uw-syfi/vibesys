package production

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// gitIn runs git in dir with an identity and no configuration of the machine's.
func gitIn(t *testing.T, dir string, args ...string) {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-C", dir, "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"}, args...)...)
	cmd.Env = append(os.Environ(), "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_SYSTEM=/dev/null")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git %v in %s: %v: %s", args, dir, err, out)
	}
}

func write(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// newRepository is a git work tree with a task folder holding one file, and no commit.
func newRepository(t *testing.T) string {
	t.Helper()
	dir, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	gitIn(t, dir, "init", "-q")
	write(t, filepath.Join(dir, ".vibesys", "tasks", "demo", "OBJECTIVE.md"), "go faster\n")
	return dir
}

func commitAll(t *testing.T, dir string) {
	t.Helper()
	gitIn(t, dir, "add", "-A")
	gitIn(t, dir, "commit", "-q", "-m", "c")
}

// Subagents work in copies of HEAD, so a repository with no commit, or whose task is not in HEAD,
// is refused at the start, naming the path and what to do. Everything else a lead can cope with is
// accepted: uncommitted edits to the task, other untracked files, a detached HEAD.
func TestAStartNeedsACommitHoldingTheTask(t *testing.T) {
	t.Run("a repository with no commit", func(t *testing.T) {
		dir := newRepository(t)
		_, err := realChecks.CheckStart(dir, "demo")
		r := refusal(t, err)
		for _, w := range []string{dir, "no commit", "commit"} {
			if !strings.Contains(r.Reason, w) {
				t.Errorf("reason %q lacks %q", r.Reason, w)
			}
		}
	})
	t.Run("a task that is only in the working tree", func(t *testing.T) {
		dir := newRepository(t)
		write(t, filepath.Join(dir, "README"), "x")
		gitIn(t, dir, "add", "README")
		gitIn(t, dir, "commit", "-q", "-m", "c")
		_, err := realChecks.CheckStart(dir, "demo")
		r := refusal(t, err)
		for _, w := range []string{dir, `"demo"`, "not committed", "git add .vibesys/tasks/demo"} {
			if !strings.Contains(r.Reason, w) {
				t.Errorf("reason %q lacks %q", r.Reason, w)
			}
		}
	})
	t.Run("a task that is staged but not committed", func(t *testing.T) {
		dir := newRepository(t)
		write(t, filepath.Join(dir, "README"), "x")
		gitIn(t, dir, "add", "README")
		gitIn(t, dir, "commit", "-q", "-m", "c")
		gitIn(t, dir, "add", ".vibesys")
		if _, err := realChecks.CheckStart(dir, "demo"); refusal(t, err) == nil {
			t.Fatal("accepted")
		}
	})
	t.Run("a committed task", func(t *testing.T) {
		dir := newRepository(t)
		commitAll(t, dir)
		got, err := realChecks.CheckStart(dir, "demo")
		if err != nil || got != dir {
			t.Fatalf("got %q, %v, want %q accepted", got, err, dir)
		}
	})
	t.Run("uncommitted edits and untracked files", func(t *testing.T) {
		dir := newRepository(t)
		commitAll(t, dir)
		write(t, filepath.Join(dir, ".vibesys", "tasks", "demo", "OBJECTIVE.md"), "changed\n")
		write(t, filepath.Join(dir, "untracked"), "x")
		if _, err := realChecks.CheckStart(dir, "demo"); err != nil {
			t.Fatalf("got %v, want it accepted: the optimize skill judges uncommitted changes", err)
		}
	})
	t.Run("a detached HEAD", func(t *testing.T) {
		dir := newRepository(t)
		commitAll(t, dir)
		gitIn(t, dir, "checkout", "-q", "--detach")
		if _, err := realChecks.CheckStart(dir, "demo"); err != nil {
			t.Fatalf("got %v, want a detached HEAD accepted", err)
		}
	})
	t.Run("a task whose name is a glob", func(t *testing.T) {
		dir := newRepository(t)
		commitAll(t, dir)
		if _, err := realChecks.CheckStart(dir, "*"); err == nil {
			t.Fatal("accepted a task named *, which is not a folder")
		}
	})
}
