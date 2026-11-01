package filestores_test

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"testing/quick"

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
	return filestores.New(index, app.Rules(filestores.DefaultSamples))
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

// A repository that was deleted is not known, and listing it does not make it again.
func TestKnownLeavesOutAVanishedRepository(t *testing.T) {
	indexDir := t.TempDir()
	repo := filepath.Join(t.TempDir(), "repo")
	if err := os.Mkdir(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	stores := newStores(t, indexDir)
	if _, err := stores.Open(context.Background(), app.Scope{Repository: repo, Task: "kv-store"}); err != nil {
		t.Fatal(err)
	}
	if err := os.RemoveAll(repo); err != nil {
		t.Fatal(err)
	}
	known, err := newStores(t, indexDir).Known(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(known) != 0 {
		t.Fatalf("Known = %v, want none", known)
	}
	if _, err := os.Stat(repo); !os.IsNotExist(err) {
		t.Fatalf("listing the stores made %s again (stat error %v)", repo, err)
	}
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
