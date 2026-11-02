package filestore_test

import (
	"bytes"
	"errors"
	"maps"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

// populated is the filesystem of a store the whole script ran on, after a process kill.
func populated(t *testing.T) *fsys.MemFS {
	t.Helper()
	mem := fsys.NewMemFS()
	s := openOn(t, mem)
	for _, o := range script() {
		if err := o.run(t.Context(), s); err != nil {
			t.Fatalf("%s: %v", o.name, err)
		}
	}
	return mem.CrashImage(false)
}

func wantFailure(t *testing.T, what string, err error, kind core.FailureKind, names ...string) {
	t.Helper()
	var f *core.Failure
	if !errors.As(err, &f) {
		t.Errorf("%s: error = %v, want a *core.Failure of kind %s", what, err, kind)
		return
	}
	if f.Kind != kind {
		t.Errorf("%s: failure kind = %s (%s), want %s", what, f.Kind, f.Message, kind)
	}
	for _, name := range names {
		if !strings.Contains(f.Message, name) {
			t.Errorf("%s: the message %q does not name %q", what, f.Message, name)
		}
	}
}

func file(t *testing.T, mem *fsys.MemFS, name string) []byte {
	t.Helper()
	data, ok := mem.Files()[name]
	if !ok {
		t.Fatalf("the store has no file %s", name)
	}
	return data
}

// The files of a store, by the part of the script that makes each.
var recordFiles = []string{
	"state/jobs/j1.json", "state/jobs/j3.json", "state/results/j1.json", "state/plans/1.json", "state/promotions/1.json", "state/promotions/2.json",
}

const eventFileName = "state/runs/run-a/1.json"

func TestNewerFormatIsRefusedWithTheFileNamed(t *testing.T) {
	t.Run("the layout", func(t *testing.T) {
		mem := populated(t)
		mem.Put("state/format", []byte(`{"format":2}`))
		_, err := filestore.Open(stateDir, filestore.WithFS(mem))
		wantFailure(t, "newer format file", err, core.FailureIncompatible, "state/format")
	})

	for _, name := range recordFiles {
		t.Run(name, func(t *testing.T) {
			mem := populated(t)
			mem.Put(name, bytes.Replace(file(t, mem, name), []byte(`"format":1`), []byte(`"format":2`), 1))
			_, err := filestore.Open(stateDir, filestore.WithFS(mem))
			wantFailure(t, "newer record", err, core.FailureIncompatible, name)
		})
	}

	t.Run("an event, read when asked for", func(t *testing.T) {
		mem := populated(t)
		mem.Put(eventFileName, bytes.Replace(file(t, mem, eventFileName), []byte(`"format":1`), []byte(`"format":2`), 1))
		s, err := filestore.Open(stateDir, filestore.WithFS(mem))
		if err != nil {
			t.Fatalf("Open: %v", err)
		}
		_, err = s.Events(t.Context(), "run-a", 0, 0)
		wantFailure(t, "Events", err, core.FailureIncompatible, eventFileName)
		if _, err := s.Events(t.Context(), "run-b", 0, 0); err != nil {
			t.Errorf("the other run's events are unreadable too: %v", err)
		}
	})
}

func TestDamagedFilesAreRefusedWithTheFileNamed(t *testing.T) {
	damage := map[string]func(valid []byte) []byte{
		"not json":     func([]byte) []byte { return []byte("not json") },
		"empty":        func([]byte) []byte { return nil },
		"truncated":    func(v []byte) []byte { return v[:len(v)/2] },
		"no version":   func([]byte) []byte { return []byte(`{"unrelated":1}`) },
		"version zero": func([]byte) []byte { return []byte(`{"format":0}`) },
		"no body":      func([]byte) []byte { return []byte(`{"format":1}`) },
		"an unknown field": func(v []byte) []byte {
			return bytes.Replace(v, []byte(`"format":1`), []byte(`"format":1,"surprise":true`), 1)
		},
		"data after it":     func(v []byte) []byte { return append(bytes.Clone(v), []byte(`{"format":1}`)...) },
		"a wrong type":      func(v []byte) []byte { return bytes.Replace(v, []byte(`"format":1`), []byte(`"format":"one"`), 1) },
		"binary":            func([]byte) []byte { return []byte{0, 1, 2, 0xff, 0xfe} },
		"a different field": func(v []byte) []byte { return bytes.Replace(v, []byte(`"format"`), []byte(`"formats"`), 1) },
	}
	for _, name := range append([]string{"state/format"}, recordFiles...) {
		for what, damaged := range damage {
			if name == "state/format" && what == "no body" {
				continue // the format file has nothing but its version
			}
			t.Run(name+" "+what, func(t *testing.T) {
				mem := populated(t)
				mem.Put(name, damaged(file(t, mem, name)))
				_, err := filestore.Open(stateDir, filestore.WithFS(mem))
				wantFailure(t, what, err, core.FailureInternal, name)
			})
		}
	}

	t.Run("an event, read when asked for", func(t *testing.T) {
		for what, damaged := range damage {
			mem := populated(t)
			mem.Put(eventFileName, damaged(file(t, mem, eventFileName)))
			s, err := filestore.Open(stateDir, filestore.WithFS(mem))
			if err != nil {
				t.Fatalf("%s: Open: %v", what, err)
			}
			_, err = s.Events(t.Context(), "run-a", 0, 0)
			wantFailure(t, what, err, core.FailureInternal, eventFileName)
		}
	})
}

func TestInconsistentLayoutIsRefusedWithTheFileNamed(t *testing.T) {
	tests := []struct {
		name   string
		break_ func(mem *fsys.MemFS)
		names  []string
	}{
		{"a job under another job's name", func(m *fsys.MemFS) { m.Put("state/jobs/j2.json", file(t, m, "state/jobs/j1.json")) }, []string{"state/jobs/j2.json"}},
		{"a job id the store does not assign", func(m *fsys.MemFS) { m.Put("state/jobs/x1.json", file(t, m, "state/jobs/j1.json")) }, []string{"state/jobs/x1.json"}},
		{"a missing job", func(m *fsys.MemFS) {
			// Nothing else may refer to the job, or another check than the gap would answer.
			mustRemove(t, m, "state/jobs/j2.json")
			mustRemove(t, m, "state/logs/j2/00000000000000000000")
			mustRemove(t, m, "state/logs/j2")
		}, []string{"j2 is missing"}},
		{"a missing plan", func(m *fsys.MemFS) {
			m.Put("state/plans/3.json", bytes.Replace(file(t, m, "state/plans/1.json"), []byte(`"number":1`), []byte(`"number":3`), 1))
		}, []string{"plan 2 is missing"}},
		{"a plan under another plan's number", func(m *fsys.MemFS) { m.Put("state/plans/2.json", file(t, m, "state/plans/1.json")) }, []string{"state/plans/2.json"}},
		{"a missing promotion", func(m *fsys.MemFS) { mustRemove(t, m, "state/promotions/1.json") }, []string{"state/promotions/1.json"}},
		{"a missing event", func(m *fsys.MemFS) { mustRemove(t, m, "state/runs/run-a/1.json") }, []string{"state/runs/run-a/1.json"}},
		{"a missing first log chunk", func(m *fsys.MemFS) { mustRemove(t, m, "state/logs/j1/00000000000000000000") }, []string{"state/logs/j1/00000000000000000006"}},
		{"a job that names a result that is not there", func(m *fsys.MemFS) { mustRemove(t, m, "state/results/j1.json") }, []string{"j1"}},
		{"a result whose job did not succeed", func(m *fsys.MemFS) {
			m.Put("state/jobs/j1.json", bytes.Replace(file(t, m, "state/jobs/j1.json"), []byte(`"state":"succeeded"`), []byte(`"state":"failed"`), 1))
		}, []string{"j1", "failed"}},
		{"a file that is not a record in jobs", func(m *fsys.MemFS) { m.Put("state/jobs/notes.txt", []byte("hello")) }, []string{"state/jobs/notes.txt"}},
		{"a record with no suffix in plans", func(m *fsys.MemFS) { m.Put("state/plans/3", []byte("{}")) }, []string{"state/plans/3"}},
		{"a plan number with a leading zero", func(m *fsys.MemFS) { m.Put("state/plans/01.json", file(t, m, "state/plans/1.json")) }, []string{"state/plans/01.json"}},
		{"a file where a run directory belongs", func(m *fsys.MemFS) { m.Put("state/runs/stray.json", []byte("{}")) }, []string{"state/runs/stray.json"}},
		{"a log chunk with a wrong name", func(m *fsys.MemFS) { m.Put("state/logs/j1/chunk", []byte("x")) }, []string{"state/logs/j1/chunk"}},
		{"an empty log chunk", func(m *fsys.MemFS) { m.Put("state/logs/j1/00000000000000000011", nil) }, []string{"state/logs/j1/00000000000000000011"}},
		{"a log with a gap", func(m *fsys.MemFS) { m.Put("state/logs/j1/00000000000000000099", []byte("x")) }, []string{"state/logs/j1/00000000000000000099"}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			mem := populated(t)
			tc.break_(mem)
			_, err := filestore.Open(stateDir, filestore.WithFS(mem))
			if tc.names == nil {
				wantFailure(t, tc.name, err, core.FailureInternal)
				return
			}
			wantFailure(t, tc.name, err, core.FailureInternal, tc.names...)
		})
	}
}

func mustRemove(t *testing.T, f fsys.FS, name string) {
	t.Helper()
	if err := f.Remove(name); err != nil {
		t.Fatalf("Remove(%s): %v", name, err)
	}
}

func TestLogChunkDamagedAfterOpeningIsReportedWhenRead(t *testing.T) {
	mem := populated(t)
	s := openOn(t, mem)
	name := "state/logs/j1/00000000000000000000"
	mem.Put(name, []byte("hel"))
	_, err := s.ReadLog(t.Context(), "j1", 0, 0)
	wantFailure(t, "ReadLog", err, core.FailureInternal, name)
}

func TestNonStoreDirectoryIsRefused(t *testing.T) {
	t.Run("a directory with other files and no format file", func(t *testing.T) {
		mem := fsys.NewMemFS()
		mem.Put("work/readme.txt", []byte("mine"))
		_, err := filestore.Open("work", filestore.WithFS(mem))
		wantFailure(t, "Open", err, core.FailureInvalidInput, "work", "readme.txt")
		if got := mem.Files(); len(got) != 1 {
			t.Errorf("Open wrote into a directory it refused: %v", got)
		}
	})

	t.Run("a path that is a file", func(t *testing.T) {
		mem := fsys.NewMemFS()
		mem.Put("work", []byte("a file"))
		_, err := filestore.Open("work", filestore.WithFS(mem))
		wantFailure(t, "Open", err, core.FailureUnavailable, "work")
	})

	t.Run("an empty directory becomes a store, and so does one with only hidden files", func(t *testing.T) {
		mem := fsys.NewMemFS()
		mem.Put("work/.keep", nil)
		s, err := filestore.Open("work", filestore.WithFS(mem))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := s.Submit(t.Context(), core.Submission{Spec: measureSpec("c1"), Key: "k1", At: at(1)}); err != nil {
			t.Fatal(err)
		}
		if err := s.Close(); err != nil {
			t.Fatal(err)
		}
		if _, err := filestore.Open("work", filestore.WithFS(mem)); err != nil {
			t.Errorf("reopening: %v", err)
		}
	})

	t.Run("opening twice leaves the directory as it was, but for who owns it", func(t *testing.T) {
		mem := fsys.NewMemFS()
		s := openOn(t, mem)
		if err := s.Close(); err != nil {
			t.Fatal(err)
		}
		before := mem.Files()
		openOn(t, mem)
		after := mem.Files()
		delete(before, "state/.owner")
		delete(after, "state/.owner")
		if !reflect.DeepEqual(before, after) {
			t.Errorf("a second Open changed the directory:\nbefore %v\nafter  %v", before, after)
		}
	})
}

func TestLeftoversOfACrashAreRemovedAndOrphansIgnored(t *testing.T) {
	t.Run("temporary files", func(t *testing.T) {
		mem := populated(t)
		strays := []string{"state/jobs/.tmp-3", "state/runs/run-a/.tmp-0", "state/logs/j1/.tmp-9", "state/results/.tmp-1"}
		for _, name := range strays {
			mem.Put(name, []byte("half a rec"))
		}
		mem.Put("state/jobs/.nfs0001", []byte("left by a network filesystem"))
		before := storetest.Observe(t, openOn(t, populated(t)), runs...)
		s := openOn(t, mem)
		for _, name := range strays {
			if _, there := mem.Files()[name]; there {
				t.Errorf("%s is still there after Open", name)
			}
		}
		if _, there := mem.Files()["state/jobs/.nfs0001"]; !there {
			t.Errorf("Open removed a file that is not its own")
		}
		if got := storetest.Observe(t, s, runs...); !reflect.DeepEqual(got, before) {
			t.Errorf("leftovers changed what the store shows:\n%+v\nwant\n%+v", got, before)
		}
	})

	t.Run("a result whose job was not published", func(t *testing.T) {
		ref := referenceRun(t)
		full := populated(t)
		// Run the script up to the call that finishes j1, then plant the result that call wrote.
		mem := fsys.NewMemFS()
		s := openOn(t, mem)
		for _, o := range ref.ops[:5] {
			if err := o.run(t.Context(), s); err != nil {
				t.Fatal(err)
			}
		}
		mem.Put("state/results/j1.json", file(t, full, "state/results/j1.json"))
		reopened, i, ok := reopen(t, ref, "with an orphan result", mem.CrashImage(false), where{done: 5})
		if !ok || i != 5 {
			t.Fatalf("the orphan changed what the store shows (state %d)", i)
		}
		carryOn(t, ref, "with an orphan result", reopened, 5)
	})
}

// The encoding is pinned: a change to what is written is a change to the format, and must come
// with a version.
func TestEncodingIsPinned(t *testing.T) {
	mem := fsys.NewMemFS()
	s := openOn(t, mem)
	if err := script()[0].run(t.Context(), s); err != nil {
		t.Fatal(err)
	}
	if got, want := string(file(t, mem, "state/format")), "{\"format\":1}\n"; got != want {
		t.Errorf("format file = %q, want %q", got, want)
	}
	const wantJob = `{"format":1,"job":{"id":"j1","spec":{"commit":"c1","environment":"","settings":{"kind":"measure","measure":{"samples":3,"recheck":false}}},` +
		`"state":"queued","key":"k1","environment":null,"notes":[{"text":"first","at":"2026-11-01T00:00:01Z","agent":"agent-a"}],"plans":null,` +
		`"submitted_at":"2026-11-01T00:00:01Z","started_at":null,"finished_at":null,"cancel_reason":"","exit_code":null,"result_job":"","check_job":""}}` + "\n"
	if got := string(file(t, mem, "state/jobs/j1.json")); got != wantJob {
		t.Errorf("job file =\n%s\nwant\n%s", got, wantJob)
	}
	if got, want := slices.Sorted(maps.Keys(mem.Files())), []string{"state/.owner", "state/.owner.lock", "state/format", "state/jobs/j1.json"}; !slices.Equal(got, want) {
		t.Errorf("files = %v, want %v", got, want)
	}

	if err := promotion("a promotion", "c1", "c0", 13).run(t.Context(), s); err != nil {
		t.Fatal(err)
	}
	const wantPromotion = `{"format":1,"seq":1,"promotion":{"commit":"c1","previous_head":"c0","verdict":{"kind":"better","reason":"","candidate_samples":3,` +
		`"base_samples":3,"candidate_mean":2.5,"base_mean":2,"ratio":1.25,"noise":0.5},"environment":{"kind":"host","backend":"cpu","image_digest":"","facts":null},` +
		`"result_job":"j1","at":"2026-11-01T00:00:13Z"}}` + "\n"
	if got := string(file(t, mem, "state/promotions/1.json")); got != wantPromotion {
		t.Errorf("promotion file =\n%s\nwant\n%s", got, wantPromotion)
	}
}

// A store that ran to the end holds exactly these files, and no temporary one.
func TestLayoutOfAFinishedScript(t *testing.T) {
	want := []string{
		"state/.owner", "state/.owner.lock", "state/format",
		"state/jobs/j1.json", "state/jobs/j2.json", "state/jobs/j3.json",
		"state/logs/j1/00000000000000000000", "state/logs/j1/00000000000000000006", "state/logs/j2/00000000000000000000",
		"state/plans/1.json",
		"state/promotions/1.json", "state/promotions/2.json",
		"state/results/j1.json",
		"state/runs/run-a/1.json", "state/runs/run-a/2.json", "state/runs/run-b/1.json",
	}
	if got := slices.Sorted(maps.Keys(populated(t).Files())); !slices.Equal(got, want) {
		t.Errorf("files =\n%s\nwant\n%s", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
}
