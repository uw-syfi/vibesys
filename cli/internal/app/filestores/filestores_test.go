package filestores_test

import (
	"context"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"testing/quick"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/filestores"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
)

func newStores(t *testing.T, indexDir string) *filestores.FileStores {
	t.Helper()
	index, err := filestore.OpenIndex(indexDir)
	if err != nil {
		t.Fatal(err)
	}
	return filestores.New(index, app.Rules(filestores.DefaultSamples), filestore.Owner{})
}

// A scope opened once is found by a daemon that starts later, over the same index.
func TestAnOpenedScopeIsKnownAfterARestart(t *testing.T) {
	repo, indexDir := t.TempDir(), t.TempDir()
	first := newStores(t, indexDir)
	scope := app.Scope{Repository: repo, Task: "kv-store"}
	if _, err := first.Open(context.Background(), scope); err != nil {
		t.Fatal(err)
	}
	known, err := newStores(t, indexDir).Known(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(known) != 1 || known[0] != scope {
		t.Fatalf("Known = %v, want [%v]", known, scope)
	}
}

// Opening a scope again hands back the same store, so there is one writer in the process.
func TestOpenSharesTheStore(t *testing.T) {
	stores := newStores(t, t.TempDir())
	scope := app.Scope{Repository: t.TempDir(), Task: "kv-store"}
	a, err := stores.Open(context.Background(), scope)
	if err != nil {
		t.Fatal(err)
	}
	b, err := stores.Open(context.Background(), scope)
	if err != nil {
		t.Fatal(err)
	}
	if a != b {
		t.Fatal("two Opens of one scope returned different stores")
	}
}

// A repository that is away stays known, and neither listing it nor trying it makes anything, so
// what it holds is all there when it returns.
func TestARepositoryThatIsAwayIsKnownAndComesBackWhole(t *testing.T) {
	awayWays := map[string]func(t *testing.T, repo string) (back func()){
		"the repository is gone": func(t *testing.T, repo string) func() {
			moved := repo + ".moved"
			must(t, os.Rename(repo, moved))
			return func() { must(t, os.Rename(moved, repo)) }
		},
		"the repository is an empty mount point": func(t *testing.T, repo string) func() {
			moved := repo + ".moved"
			must(t, os.Rename(repo, moved))
			must(t, os.Mkdir(repo, 0o755))
			return func() {
				must(t, os.Remove(repo))
				must(t, os.Rename(moved, repo))
			}
		},
	}
	for what, away := range awayWays {
		t.Run(what, func(t *testing.T) {
			ctx := context.Background()
			indexDir := t.TempDir()
			repo := filepath.Join(t.TempDir(), "repo")
			must(t, os.Mkdir(repo, 0o755))
			scope := app.Scope{Repository: repo, Task: "kv-store"}

			first := newStores(t, indexDir)
			st, err := first.Open(ctx, scope)
			must(t, err)
			out, err := st.Submit(ctx, core.Submission{
				Spec: core.JobSpec{Commit: "c1", Settings: core.CommandSettings{Argv: []string{"true"}}}, At: time.Unix(1, 0).UTC(),
			})
			must(t, err)
			must(t, first.Close())

			back := away(t, repo)
			restarted := newStores(t, indexDir)
			known, err := restarted.Known(ctx)
			must(t, err)
			if len(known) != 1 || known[0] != scope {
				t.Fatalf("Known = %v, want [%v]", known, scope)
			}
			before := listing(t, filepath.Dir(repo))
			_, err = restarted.Open(ctx, scope)
			var f *core.Failure
			if !errors.As(err, &f) || f.Kind != core.FailureUnavailable || !strings.Contains(f.Message, repo) {
				t.Fatalf("Open of a store that is away: %v, want unavailable naming %s", err, repo)
			}
			if after := listing(t, filepath.Dir(repo)); !slices.Equal(before, after) {
				t.Fatalf("trying a store that is away made something:\nbefore %v\nafter  %v", before, after)
			}

			back()
			st, err = restarted.Open(ctx, scope)
			must(t, err)
			job, err := st.Job(ctx, out.Job.ID)
			must(t, err)
			if job.ID != out.Job.ID {
				t.Fatalf("after the return: job %s, want %s", job.ID, out.Job.ID)
			}
		})
	}
}

// A store that was never made needs a repository to be made in: a path with a mistake in it makes
// nothing.
func TestAFirstOpenMakesNoRepository(t *testing.T) {
	repo := filepath.Join(t.TempDir(), "typo")
	_, err := newStores(t, t.TempDir()).Open(context.Background(), app.Scope{Repository: repo, Task: "kv-store"})
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
		t.Fatalf("Open: %v, want unavailable", err)
	}
	if _, err := os.Stat(repo); !os.IsNotExist(err) {
		t.Fatalf("Open made %s (stat error %v)", repo, err)
	}
}

// Two sets of stores are two daemons, whatever else separates them.
func TestASecondDaemonIsRefusedAStoreAndNamesWhoHasIt(t *testing.T) {
	ctx := context.Background()
	scope := app.Scope{Repository: t.TempDir(), Task: "kv-store"}
	index, err := filestore.OpenIndex(t.TempDir())
	must(t, err)
	rules := app.Rules(filestores.DefaultSamples)
	first := filestores.New(index, rules, filestore.Owner{Host: "host-a", Dir: "/run/one", PID: 1})
	_, err = first.Open(ctx, scope)
	must(t, err)
	second := filestores.New(index, rules, filestore.Owner{Host: "host-a", Dir: "/run/two", PID: 2})
	_, err = second.Open(ctx, scope)
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureConflict || !strings.Contains(f.Message, "/run/one") {
		t.Fatalf("second Open: %v, want a conflict naming the first", err)
	}
	must(t, first.Close())
	_, err = second.Open(ctx, scope)
	must(t, err)
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func listing(t *testing.T, dir string) []string {
	t.Helper()
	var out []string
	must(t, filepath.WalkDir(dir, func(name string, _ fs.DirEntry, err error) error {
		out = append(out, name)
		return err
	}))
	return out
}

// A scope that is not a path and a task name is refused with the field named, whatever it is.
func TestOpenRefusesAScopeThatIsNotOne(t *testing.T) {
	stores := newStores(t, t.TempDir())
	property := func(repository, task string) bool {
		scope := app.Scope{Repository: repository, Task: task}
		if scope.Validate() == nil {
			return true
		}
		_, err := stores.Open(context.Background(), scope)
		f, ok := err.(*core.Failure)
		return ok && f.Kind == core.FailureInvalidInput
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

// The rules reach the store: a submission that leaves the sample count at zero joins a queued
// measurement of the task's default count.
func TestTheStoreIsOpenedWithTheRules(t *testing.T) {
	stores := newStores(t, t.TempDir())
	scope := app.Scope{Repository: t.TempDir(), Task: "kv-store"}
	st, err := stores.Open(context.Background(), scope)
	if err != nil {
		t.Fatal(err)
	}
	commit := core.Commit("0123456789abcdef0123456789abcdef01234567")
	sub := func(samples uint32) core.Submission {
		return core.Submission{
			Context: core.CallContext{Repository: scope.Repository, Task: scope.Task},
			Spec:    core.JobSpec{Commit: commit, Settings: core.MeasureSettings{Samples: samples}},
			Key:     "measure-key",
		}
	}
	first, err := st.Submit(context.Background(), sub(filestores.DefaultSamples))
	if err != nil {
		t.Fatal(err)
	}
	second, err := st.Submit(context.Background(), sub(0))
	if err != nil {
		t.Fatal(err)
	}
	if second.Disposition != core.SubmitJoined || second.Job.ID != first.Job.ID {
		t.Fatalf("a submission with no sample count made %s job %s, want to join %s", second.Disposition, second.Job.ID, first.Job.ID)
	}
}
