package filestore_test

import (
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"google.golang.org/protobuf/encoding/protojson"
	"io/fs"
	"os"
	"path/filepath"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
)

// This fixture was exported by the preceding implementation at fa9d95d1f^,
// using its full store script, including results, promotions, plans and logs.
func TestPreviousVersionStoreLoadsWithoutInventingSubmitters(t *testing.T) {
	mem := previousStore(t)
	index, err := filestore.OpenIndex("index", filestore.WithFS(mem))
	if err != nil {
		t.Fatal(err)
	}
	tasks, err := index.Tasks()
	if err != nil || len(tasks) != 1 || tasks[0].Repository != "/repo" || tasks[0].Name != "task" {
		t.Fatalf("index = %v, %v", tasks, err)
	}
	s := openOn(t, mem)
	jobs, err := s.Jobs(t.Context(), core.JobFilter{})
	if err != nil {
		t.Fatal(err)
	}
	if len(jobs) != 3 {
		t.Fatalf("jobs = %v", jobs)
	}
	for _, j := range jobs {
		if len(j.Submitters) != 0 {
			t.Fatalf("invented submitters: %+v", j)
		}
	}
	if rs, err := s.Results(t.Context(), core.ResultFilter{}); err != nil || len(rs) != 1 {
		t.Fatalf("results = %v, %v", rs, err)
	}
	if ps, err := s.Promotions(t.Context()); err != nil || len(ps) != 2 {
		t.Fatalf("promotions = %v, %v", ps, err)
	}
	if ps, err := s.Plans(t.Context(), core.PlanFilter{}); err != nil || len(ps) != 1 {
		t.Fatalf("plans = %v, %v", ps, err)
	}
	if log, err := s.ReadLog(t.Context(), "j1", 0, 0); err != nil || len(log.Data) == 0 {
		t.Fatalf("log = %v, %v", log, err)
	}
}

func FuzzPreviousVersionJob(f *testing.F) {
	f.Add("historical agent")
	f.Fuzz(func(t *testing.T, agent string) {
		// Generated old records have no submitters. Vary a historical note without
		// accidentally relying on the new writer to produce the old encoding.
		mem := previousStore(t)
		data := file(t, mem, "state/jobs/j1.json")
		var record cliv1.JobFile
		if err := protojson.Unmarshal(data, &record); err != nil {
			t.Fatal(err)
		}
		record.Job.Notes = []*cliv1.Note{{Text: agent}}
		data, err := protojson.Marshal(&record)
		if err != nil {
			t.Skip("not protobuf text")
		}
		mem.Put("state/jobs/j1.json", data)
		s := openOn(t, mem)
		j, err := s.Job(t.Context(), "j1")
		if err != nil || len(j.Submitters) != 0 {
			t.Fatalf("job = %+v, %v", j, err)
		}
	})
}

// previousStore loads each preceding writer's bytes without defining another
// record format in the test.
func previousStore(t testing.TB) *fsys.MemFS {
	t.Helper()
	mem := fsys.NewMemFS()
	err := fs.WalkDir(os.DirFS("testdata/format2"), ".", func(name string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			return nil
		}
		data, err := os.ReadFile(filepath.Join("testdata/format2", name))
		if err == nil {
			mem.Put(name, data)
		}
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	return mem
}
