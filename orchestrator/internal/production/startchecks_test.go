package production

import (
	"errors"
	"math/rand/v2"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// fakeTree is a file system of paths; a true value is a directory and a false one a file.
type fakeTree map[string]bool

func (t fakeTree) Exists(path string) bool { _, ok := t[path]; return ok }
func (t fakeTree) IsDir(path string) bool  { return t[path] }

// A fakeTree has no links and every repository in it is committed, task included, so that only the
// rules about paths can refuse; osTree_test.go runs the history rules against real repositories.
func (t fakeTree) RealPath(path string) (string, error)      { return path, nil }
func (t fakeTree) HasCommit(string) (bool, error)            { return true, nil }
func (t fakeTree) CommittedDir(string, string) (bool, error) { return true, nil }

// readyLauncher is a launcher that is wired in; what it launches does not matter here.
type readyLauncher struct{ NoSidecars }

// readyParts has every part, and brokenParts lacks one, with a reason.
type readyParts struct{}

func (readyParts) Check() error { return nil }

type brokenParts string

func (b brokenParts) Check() error { return errors.New(string(b)) }

// everythingIsADir answers yes to every question, so that only the rules of StartChecks themselves
// can refuse.
type everythingIsADir struct{}

func (everythingIsADir) Exists(string) bool { return true }
func (everythingIsADir) IsDir(string) bool  { return true }

func (everythingIsADir) RealPath(path string) (string, error)      { return path, nil }
func (everythingIsADir) HasCommit(string) (bool, error)            { return true, nil }
func (everythingIsADir) CommittedDir(string, string) (bool, error) { return true, nil }

func refusal(t *testing.T, err error) *daemon.StartRefusal {
	t.Helper()
	var r *daemon.StartRefusal
	if !errors.As(err, &r) {
		t.Fatalf("got %v, want a *daemon.StartRefusal", err)
	}
	return r
}

func TestAStartNeedsAnAbsoluteGitWorkTreeHoldingTheTask(t *testing.T) {
	tree := fakeTree{"/repo/.git": true, "/repo/.vibesys/tasks/demo": true, "/wt/.git": false, "/wt/.vibesys/tasks/demo": true, "/nogit/.vibesys/tasks/demo": true, "/notask/.git": true, "/notask/.vibesys/tasks/file": false}
	checks := StartChecks{Sidecars: readyLauncher{}, Parts: readyParts{}, Tree: tree}
	cases := []struct {
		name, repository, task string
		want                   []string // words the refusal names; none means accepted
	}{
		{"a repository with the task", "/repo", "demo", nil},
		{"a linked worktree has a .git file", "/wt", "demo", nil},
		{"a relative repository", "repo", "demo", []string{`"repo"`, "not an absolute path"}},
		{"an empty repository", "", "demo", []string{`""`, "not an absolute path"}},
		{"no such repository", "/missing", "demo", []string{"/missing", "not a git work tree"}},
		{"no .git", "/nogit", "demo", []string{"/nogit", "not a git work tree"}},
		{"no such task", "/repo", "other", []string{`"other"`, "/repo/.vibesys/tasks/other"}},
		{"a task that is a file", "/notask", "file", []string{`"file"`, "/notask/.vibesys/tasks/file"}},
		{"an empty task", "/repo", "", []string{`""`, "not a task name"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := checks.CheckStart(c.repository, c.task)
			if c.want == nil {
				if err != nil {
					t.Fatalf("got %v, want it accepted", err)
				}
				return
			}
			r := refusal(t, err)
			if r.NotReady {
				t.Errorf("%v is marked NotReady, but the request is at fault", r)
			}
			for _, w := range c.want {
				if !strings.Contains(r.Reason, w) {
					t.Errorf("reason %q lacks %q", r.Reason, w)
				}
			}
		})
	}
}

// CheckRepository is the repository part of CheckStart and nothing else: it refuses what CheckStart
// refuses before it looks at the task, with the same words, and accepts a repository whatever the
// task is (a task still being typed, one the repository does not hold).
func TestCheckRepositoryAsksTheRepositoryAloneAndRefusesAsAStartDoes(t *testing.T) {
	tree := fakeTree{"/repo/.git": true, "/nogit/.vibesys/tasks/demo": true}
	checks := StartChecks{Sidecars: readyLauncher{}, Parts: readyParts{}, Tree: tree}
	for _, repository := range []string{"/repo", "/nogit", "/missing", "relative", ""} {
		_, repositoryErr := checks.CheckRepository(repository)
		for _, task := range []string{"demo", "", "../x", "absent"} {
			_, startErr := checks.CheckStart(repository, task)
			if repositoryErr != nil {
				if startErr == nil || startErr.Error() != repositoryErr.Error() {
					t.Fatalf("%q with task %q: CheckStart gave %v, want CheckRepository's refusal %v", repository, task, startErr, repositoryErr)
				}
			}
		}
		if repositoryErr == nil && repository != "/repo" {
			t.Fatalf("%q was accepted, want only /repo", repository)
		}
		if repositoryErr != nil && refusal(t, repositoryErr).NotReady {
			t.Fatalf("%q: the repository is at fault, not the daemon", repository)
		}
	}
}

// A daemon with a part missing refuses every start, with the part's own reason, before it looks at
// the repository.
func TestADaemonWithAPartMissingRefusesEveryStartWithItsReason(t *testing.T) {
	checks := StartChecks{Sidecars: readyLauncher{}, Parts: brokenParts("no session holder is configured and there is no release"), Tree: everythingIsADir{}}
	for _, repository := range []string{"/repo", "relative", ""} {
		_, err := checks.CheckStart(repository, "demo")
		r := refusal(t, err)
		if !r.NotReady || !strings.Contains(r.Reason, "no session holder is configured and there is no release") {
			t.Fatalf("%q: got %+v, want a NotReady refusal with the launcher's reason", repository, r)
		}
	}
}

// Whatever the name, a task that is accepted stays under .vibesys/tasks of the repository, and a
// name that could leave it is refused. The names mix plain segments with the pieces that escape.
func TestATaskNameNeverLeavesTheTasksFolder(t *testing.T) {
	pieces := []string{"a", "demo", "..", ".", "/", "\\", "x/y", "...", " ", "\x00", "-", "~", "a.b", ".hidden"}
	tasks := filepath.Join("/repo", ".vibesys", "tasks")
	for seed := uint64(1); seed <= 2000; seed++ {
		rng := rand.New(rand.NewPCG(seed, 7))
		var name strings.Builder
		for range rng.IntN(4) {
			name.WriteString(pieces[rng.IntN(len(pieces))])
		}
		task := name.String()
		_, err := StartChecks{Sidecars: readyLauncher{}, Parts: readyParts{}, Tree: everythingIsADir{}}.CheckStart("/repo", task)
		if err != nil {
			refusal(t, err)
			continue
		}
		dir := filepath.Join(tasks, task)
		if filepath.Dir(dir) != tasks || filepath.Base(dir) != task {
			t.Fatalf("seed %d: task %q was accepted but resolves to %s, outside %s", seed, task, dir, tasks)
		}
	}
	for _, escape := range []string{"..", "../x", "a/../../x", "x/..", "/etc", "a/b", ".", ""} {
		if _, err := (StartChecks{Sidecars: readyLauncher{}, Parts: readyParts{}, Tree: everythingIsADir{}}).CheckStart("/repo", escape); err == nil {
			t.Errorf("task %q was accepted", escape)
		}
	}
}

// The Fake the daemon's tests use answers the same way a refusal does: as a *StartRefusal that the
// service turns into a status.
var _ daemon.StartChecks = (*daemontest.StartChecks)(nil)
var _ daemon.StartChecks = StartChecks{}
