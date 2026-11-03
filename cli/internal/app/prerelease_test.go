package app_test

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// A store a prerelease build wrote, recorded in the index by that build, does not stop the daemon:
// it starts, the other repository's jobs run, and a command on the old store is refused with the
// files to remove. Once they are removed, the next submission makes the store again.
func TestAPrereleaseStoreIsRefusedAloneAndTheRestWork(t *testing.T) {
	w := newWorld(t)
	index, old, oldCall := onDisk(t)
	repoNew := t.TempDir()
	fresh := scopeAt(repoNew, "task")
	freshCall := core.CallContext{Scope: fresh, Run: "run-1", Harness: "h", Agent: "agent-1"}
	owner := filestore.Owner{Host: "host-a", Dir: "/run/one", PID: 1}

	// Both stores are made, then the old one is turned into what the prerelease build left: its
	// format file and its index entry at version 1.
	first := w.newPlace(index, owner, w.survivors)
	a := w.startAt(first)
	submitIn(t, a, oldCall, "exit", "0")
	done := submitIn(t, a, freshCall, "exit", "0")
	awaitIn(t, a, fresh, done.ID, core.JobSucceeded)
	a.Close()
	if err := first.stores.Close(); err != nil {
		t.Fatal(err)
	}
	oldTask := filestore.Task{Repository: string(old.Repository()), Name: string(old.Task())}
	entry, err := index.Entry(oldTask)
	if err != nil || entry == "" {
		t.Fatalf("Entry = %q, %v", entry, err)
	}
	storeDir := filepath.Dir(filestore.OwnerRecord(oldTask.Repository, oldTask.Name))
	formatFile := filepath.Join(storeDir, "format")
	writeFile(t, formatFile, `{"format":1}`)
	writeFile(t, entry, `{"format":1,"repository":"`+oldTask.Repository+`","task":"task"}`)

	b := w.startAt(w.newPlace(index, owner, supervisor.NewSurvivors()))
	job := submitIn(t, b, freshCall, "print", "after")
	awaitIn(t, b, fresh, job.ID, core.JobSucceeded)

	_, err = b.Jobs(ctxFor(t), old, core.JobFilter{})
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureIncompatible {
		t.Fatalf("a command on the prerelease store = %v, want an incompatible failure", err)
	}
	for _, named := range []string{formatFile, storeDir, entry, "remove"} {
		if !strings.Contains(f.Message, named) {
			t.Errorf("the refusal %q does not name %q", f.Message, named)
		}
	}

	// The fix the refusal names works.
	if err := os.RemoveAll(storeDir); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(entry); err != nil {
		t.Fatal(err)
	}
	again := submitIn(t, b, oldCall, "print", "again")
	awaitIn(t, b, old, again.ID, core.JobSucceeded)
}

func writeFile(t *testing.T, name, content string) {
	t.Helper()
	if err := os.WriteFile(name, []byte(content+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}
