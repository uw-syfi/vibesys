package store_test

import (
	"fmt"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs/localfstest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store/storetest"
)

// localDir proves a test directory local without asking the host: the contract tests are about the
// store, and where TMPDIR lives is the environment's business (the refusal itself is tested in
// localfs). Real databases belong on a local disk, so a host with an NFS TMPDIR should set it.
func localDir(t *testing.T) localfs.Dir {
	t.Helper()
	dir, err := localfs.Resolve(t.TempDir(), localfstest.Fake{})
	if err != nil {
		t.Fatal(err)
	}
	return dir
}

func openSQLite(t *testing.T, dir localfs.Dir, opts store.SQLiteOptions) *store.SQLite {
	t.Helper()
	s, err := store.OpenSQLite(dir, opts)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}

func TestSQLitePassesTheContract(t *testing.T) {
	storetest.Contract(t, func(t *testing.T) *storetest.Harness {
		dir := localDir(t)
		s := openSQLite(t, dir, store.SQLiteOptions{})
		h := &storetest.Harness{Store: s}
		h.Reopen = func() store.Store {
			if err := s.Close(); err != nil {
				t.Fatal(err)
			}
			s = openSQLite(t, dir, store.SQLiteOptions{})
			return s
		}
		return h
	})
}

// planProblems says what is wrong with the plan SQLite gave for a screen read: a scan of a table, a
// temporary B-tree (which is a sort or a distinct over every row the read matches, before its limit
// can cut it short), or no search at all.
func planProblems(lines []string) []string {
	var out []string
	searched := false
	for _, line := range lines {
		switch {
		case strings.Contains(line, "SCAN") && !strings.Contains(line, "SCAN CONSTANT ROW"):
			out = append(out, "scans: "+line)
		case strings.Contains(line, "TEMP B-TREE"):
			out = append(out, "sorts: "+line)
		}
		searched = searched || strings.Contains(line, "SEARCH")
	}
	if !searched {
		out = append(out, "does not search an index")
	}
	return out
}

func TestThePlanCheckRejectsAScanAndASort(t *testing.T) {
	cases := []struct {
		name  string
		lines []string
		bad   bool
	}{
		{"an index search", []string{"SEARCH outbox USING INDEX outbox_order (state=?)"}, false},
		{"a sort after a search", []string{"SEARCH outbox USING INDEX outbox_due (state=? AND run_after<?)", "USE TEMP B-TREE FOR ORDER BY"}, true},
		{"a sort of a compound", []string{"MERGE (UNION ALL)", "LEFT", "SEARCH turns USING INDEX turns_ended (run_id=?)", "USE TEMP B-TREE FOR ORDER BY"}, true},
		{"a scan", []string{"SCAN events"}, true},
		{"no search", []string{"SCAN CONSTANT ROW"}, true},
	}
	for _, c := range cases {
		if got := planProblems(c.lines); (len(got) > 0) != c.bad {
			t.Errorf("%s: problems %v, want bad = %v", c.name, got, c.bad)
		}
	}
}

func TestScreenQueriesNeverScanOrSort(t *testing.T) {
	s := openSQLite(t, localDir(t), store.SQLiteOptions{})
	plans, err := s.Explain()
	if err != nil {
		t.Fatal(err)
	}
	if len(plans) < 11 {
		t.Fatalf("explained only %d queries: %v", len(plans), plans)
	}
	for name, lines := range plans {
		for _, problem := range planProblems(lines) {
			t.Errorf("%s %s\nplan: %v", name, problem, lines)
		}
		t.Logf("%-18s %v", name, lines)
	}
}

func TestEveryConnectionIsInWriteAheadLogModeWithItsBusyTimeout(t *testing.T) {
	s := openSQLite(t, localDir(t), store.SQLiteOptions{})
	writer, readers, err := s.Settings()
	if err != nil {
		t.Fatal(err)
	}
	if len(readers) < 2 {
		t.Fatalf("checked only %d reader connections", len(readers))
	}
	const normal = 1 // PRAGMA synchronous: 0 OFF, 1 NORMAL, 2 FULL
	want := store.Settings{JournalMode: "wal", Synchronous: normal, BusyTimeoutMS: 10000}
	if writer != want {
		t.Errorf("writer settings %+v, want %+v", writer, want)
	}
	want.QueryOnly = 1
	for i, r := range readers {
		if r != want {
			t.Errorf("reader connection %d settings %+v, want %+v", i, r, want)
		}
	}
}

// A store written under another schema version is refused, whichever way the version differs: the
// stored format has not been released, so it changes in place and old rows are not read.
func TestAStoreOfAnotherSchemaVersionIsRefused(t *testing.T) {
	for _, version := range []int{2, 3, 5} {
		dir, _ := populated(t, 1, 3)
		damage(t, dir, fmt.Sprintf(`UPDATE meta SET value = %d WHERE key = 'schema_version'`, version))
		_, err := store.OpenSQLite(dir, store.SQLiteOptions{})
		if err == nil || !strings.Contains(err.Error(), fmt.Sprintf("schema version %d", version)) {
			t.Fatalf("a store of version %d opened or failed for another reason: %v", version, err)
		}
	}
}
