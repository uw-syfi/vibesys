package filestore_test

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

const allocDir = "/state/vibesys"

func openAllocLog(t *testing.T, mem *fsys.MemFS) *filestore.AllocLog {
	t.Helper()
	l, err := filestore.OpenAllocLog(allocDir, filestore.WithFS(mem))
	if err != nil {
		t.Fatal(err)
	}
	return l
}

func TestFileAllocLogPassesTheContract(t *testing.T) {
	storetest.RunAllocLog(t, func(t *testing.T) storetest.AllocLogWorld {
		mem := fsys.NewMemFS()
		return storetest.AllocLogWorld{Log: openAllocLog(t, mem), Reopen: func() store.AllocLog { return openAllocLog(t, mem) }}
	})
}

func someInputs() []alloc.Input {
	at := time.Date(2026, 11, 10, 8, 0, 0, 0, time.UTC)
	return []alloc.Input{
		{At: at, Event: alloc.Ask{Run: "run-a", Placement: "p", For: time.Hour}},
		{At: at.Add(time.Second), Event: alloc.Submitted{ID: "a1", Handle: alloc.Handle{Alloc: "a1", Ref: "7"}}},
		{At: at.Add(2 * time.Second), Event: alloc.Tick{}},
	}
}

// A process that dies, or a call that fails, at any step of an append leaves the log as it was or
// with the input, never with a part of it, and an input appended before is never lost.
func TestAnAppendThatFailsAtAnyStepLeavesAReadableLog(t *testing.T) {
	inputs := someInputs()
	ref := fsys.NewMemFS()
	l := openAllocLog(t, ref)
	for _, in := range inputs[:2] {
		if err := l.Append(t.Context(), in); err != nil {
			t.Fatal(err)
		}
	}
	steps := ref.Steps()
	if err := l.Append(t.Context(), inputs[2]); err != nil {
		t.Fatal(err)
	}
	total := ref.Steps()
	for at := steps; at < total; at++ {
		for _, kind := range []fsys.FaultKind{fsys.FaultCrash, fsys.FaultTornWrite, fsys.FaultFail, fsys.FaultFailAfter} {
			mem := fsys.NewMemFS()
			l := openAllocLog(t, mem)
			for _, in := range inputs[:2] {
				if err := l.Append(t.Context(), in); err != nil {
					t.Fatal(err)
				}
			}
			mem.SetFault(fsys.Fault{At: at, Kind: kind})
			_ = l.Append(t.Context(), inputs[2])
			image := mem
			if kind == fsys.FaultCrash || kind == fsys.FaultTornWrite {
				image = mem.CrashImage(false)
			}
			got, err := openAllocLog(t, image).Load(t.Context())
			if err != nil {
				t.Fatalf("fault %d at step %d: Load: %v", kind, at, err)
			}
			if len(got) != 2 && len(got) != 3 {
				t.Fatalf("fault %d at step %d: the log has %d inputs, want 2 or 3", kind, at, len(got))
			}
		}
	}
}

// After a call that failed, the same input may be appended again, and the log holds it once.
func TestAnInputThatFailedMayBeAppendedAgain(t *testing.T) {
	mem := fsys.NewMemFS()
	l := openAllocLog(t, mem)
	inputs := someInputs()
	if err := l.Append(t.Context(), inputs[0]); err != nil {
		t.Fatal(err)
	}
	mem.SetFault(fsys.Fault{At: mem.Steps(), Kind: fsys.FaultFailAfter})
	if err := l.Append(t.Context(), inputs[1]); err == nil {
		t.Fatal("the injected failure did not show")
	}
	if err := l.Append(t.Context(), inputs[1]); err != nil {
		t.Fatal(err)
	}
	got, err := l.Load(t.Context())
	if err != nil || len(got) != 2 {
		t.Fatalf("Load = %d inputs, %v, want 2", len(got), err)
	}
}

// A log with a missing record is refused, naming the record, and never read past.
func TestALogWithAGapIsRefused(t *testing.T) {
	mem := fsys.NewMemFS()
	l := openAllocLog(t, mem)
	for _, in := range someInputs() {
		if err := l.Append(t.Context(), in); err != nil {
			t.Fatal(err)
		}
	}
	files := mem.Files()
	var second string
	for name := range files {
		if strings.HasSuffix(name, "/allocations/2.json") {
			second = name
		}
	}
	if second == "" {
		t.Fatalf("no input 2 among %v", files)
	}
	broken := fsys.NewMemFS()
	for name, data := range files {
		if name != second {
			broken.Put(name, data)
		}
	}
	_, err := openAllocLog(t, broken).Load(t.Context())
	var f *core.Failure
	if !errors.As(err, &f) || !strings.Contains(f.Message, "allocations/2.json") {
		t.Fatalf("Load of a log without input 2 = %v, want a refusal that names allocations/2.json", err)
	}
}

func TestADamagedRecordIsRefused(t *testing.T) {
	mem := fsys.NewMemFS()
	l := openAllocLog(t, mem)
	if err := l.Append(t.Context(), someInputs()[0]); err != nil {
		t.Fatal(err)
	}
	mem.Put(allocDir+"/allocations/1.json", []byte(`{"format":6,"seq":"1","input":{"nothing":true}}`))
	if _, err := openAllocLog(t, mem).Load(t.Context()); err == nil {
		t.Fatal("a record that is not an input was read")
	}
}

func TestTheDaemonIdentityIsMadeOnceAndKept(t *testing.T) {
	mem := fsys.NewMemFS()
	made := 0
	newID := func() string { made++; return "0123456789abcdef" }
	for range 3 {
		id, err := filestore.DaemonID(allocDir, newID, filestore.WithFS(mem))
		if err != nil || id != "0123456789abcdef" {
			t.Fatalf("DaemonID = %q, %v", id, err)
		}
	}
	if made != 1 {
		t.Fatalf("the identity was made %d times, want once", made)
	}
	mem.Put(allocDir+"/daemon-id.json", []byte(`{"format":6,"id":"short"}`))
	if _, err := filestore.DaemonID(allocDir, newID, filestore.WithFS(mem)); err == nil {
		t.Fatal("a damaged identity was read")
	}
}
