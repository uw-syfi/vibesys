package filestore_test

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
)

// This fixture was exported by the preceding implementation at fa9d95d1f^,
// using its full store script, including results, promotions, plans and logs.
func TestPreviousVersionStoreLoadsWithoutInventingSubmitters(t *testing.T) {
	data, err := os.ReadFile("testdata/format2.json")
	if err != nil {
		t.Fatal(err)
	}
	var files map[string][]byte
	if err := json.Unmarshal(data, &files); err != nil {
		t.Fatal(err)
	}
	mem := fsys.NewMemFS()
	for name, data := range files {
		mem.Put(name, data)
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
		data, err := os.ReadFile("testdata/format2.json")
		if err != nil {
			t.Fatal(err)
		}
		var files map[string][]byte
		if err := json.Unmarshal(data, &files); err != nil {
			t.Fatal(err)
		}
		var job map[string]any
		if err := json.Unmarshal(files["state/jobs/j1.json"], &job); err != nil {
			t.Fatal(err)
		}
		job["job"].(map[string]any)["notes"] = []any{map[string]any{"text": agent}}
		files["state/jobs/j1.json"], err = json.Marshal(job)
		if err != nil {
			t.Fatal(err)
		}
		mem := fsys.NewMemFS()
		for name, data := range files {
			mem.Put(name, data)
		}
		s := openOn(t, mem)
		j, err := s.Job(t.Context(), "j1")
		if err != nil || len(j.Submitters) != 0 {
			t.Fatalf("job = %+v, %v", j, err)
		}
	})
}
