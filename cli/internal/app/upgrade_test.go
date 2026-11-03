package app_test

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/filestores"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
)

func TestUpgradeRecoversPreviousWriterJobWithLiveSupervisor(t *testing.T) {
	w := newWorld(t)
	mem := fsys.NewMemFS()
	err := fs.WalkDir(os.DirFS("testdata/format2-live"), ".", func(name string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			return nil
		}
		data, err := os.ReadFile(filepath.Join("testdata/format2-live", name))
		if err == nil {
			if strings.HasPrefix(name, "repo/") {
				name = "/" + name
			}
			mem.Put(name, data)
			if strings.HasSuffix(name, "/.fixture-empty") {
				if err := mem.Remove(name); err != nil {
					return err
				}
			}
		}
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	index, err := filestore.OpenIndex("index", filestore.WithFS(mem))
	if err != nil {
		t.Fatal(err)
	}
	stores := filestores.New(index, app.Rules(5), filestore.Owner{}, filestore.WithFS(mem))
	defer stores.Close()
	sc := scopeAt("/repo", "task")
	st, err := stores.Find(t.Context(), sc)
	if err != nil {
		t.Fatal(err)
	}
	job, err := st.Job(t.Context(), "j1")
	if err != nil {
		t.Fatal(err)
	}
	if job.State != core.JobRunning || len(job.Submitters) != 0 {
		t.Fatalf("historical job = %+v", job)
	}
	// The supervisor outlives the old daemon. The replacement must attach to it,
	// rather than requeue the command or infer attribution from the run directory.
	sups := w.supervisors()
	handle, err := sups.Spawn(t.Context(), lifecycle.Launch{Repository: "/repo", Task: "task", Job: job.ID, Spec: job.Spec})
	if err != nil {
		t.Fatal(err)
	}
	defer handle.Close()
	if err := handle.Start(t.Context()); err != nil {
		t.Fatal(err)
	}
	a := w.startAt(place{stores: stores, survivors: w.survivors})
	awaitIn(t, a, sc, job.ID, core.JobRunning)
	w.gates.Release("g")
	awaitIn(t, a, sc, job.ID, core.JobSucceeded)
	got, err := st.Job(t.Context(), job.ID)
	if err != nil || len(got.Submitters) != 0 {
		t.Fatalf("recovered job = %+v, %v", got, err)
	}
}
