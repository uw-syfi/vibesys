package store_test

import (
	"database/sql"
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
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

// A store written under a version this build neither reads nor writes is refused, whichever way the
// version differs, with the advice to move the state directory away: no code converts rows. The versions
// it does read are the one it writes and the one before it, which differ by additions only.
func TestAStoreOfAnotherSchemaVersionIsRefused(t *testing.T) {
	for _, version := range []int{0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 14, 99} {
		dir, _ := populated(t, 1, 3)
		damage(t, dir, fmt.Sprintf(`UPDATE meta SET value = %d WHERE key = 'schema_version'`, version))
		_, err := store.OpenSQLite(dir, store.SQLiteOptions{})
		if err == nil || !strings.Contains(err.Error(), fmt.Sprintf("schema version %d", version)) || !strings.Contains(err.Error(), "move the state directory") {
			t.Fatalf("a store of version %d opened or failed for another reason: %v", version, err)
		}
	}
}

func storedVersion(t *testing.T, dir localfs.Dir) int {
	t.Helper()
	raw, err := sql.Open("sqlite", filepath.Join(dir.Path(), store.DatabaseFile))
	if err != nil {
		t.Fatal(err)
	}
	defer raw.Close()
	var v int
	if err := raw.QueryRow(`SELECT value FROM meta WHERE key = 'schema_version'`).Scan(&v); err != nil {
		t.Fatal(err)
	}
	return v
}

func recoverFrom(t *testing.T, dir localfs.Dir) core.State {
	t.Helper()
	s, err := store.OpenSQLite(dir, store.SQLiteOptions{})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer s.Close()
	state, err := s.Recover()
	if err != nil {
		t.Fatalf("recover: %v", err)
	}
	return state
}

// A log of version 12 that carries check-ins, as the first tag's format with the additions of the
// catch-up feature, is read by this build exactly as it was written, and the open stamps the new version
// on it before anything is replayed. The stamp is what a build that reads only 12 sees: it refuses the
// store by the number, and is never handed an event body it cannot decode. The upgrade is one-way on
// purpose, and the store is never refused for the version it came from.
func TestAStoreOfTheVersionBeforeIsReadAsItIsAndStampedSoAnOlderBuildRefusesIt(t *testing.T) {
	for seed := uint64(1); seed <= 5; seed++ {
		dir, _ := populated(t, seed, 250)
		before := recoverFrom(t, dir)
		checkIns := 0
		for _, r := range before.Runs() {
			checkIns += len(before.CheckIns(r.ID))
		}
		if checkIns == 0 {
			t.Fatalf("seed %d: the workload makes no check-ins, so this test shows nothing", seed)
		}
		damage(t, dir, `UPDATE meta SET value = 12 WHERE key = 'schema_version'`)
		after := recoverFrom(t, dir)
		if !after.Equal(before) {
			t.Fatalf("seed %d: a store of version 12 was read as another state", seed)
		}
		if got := storedVersion(t, dir); got != 13 {
			t.Fatalf("seed %d: the version after the open is %d, want 13, which a build that reads only 12 refuses", seed, got)
		}
		// Opening again changes nothing, and it is not the version 12 path any more.
		if again := recoverFrom(t, dir); !again.Equal(before) || storedVersion(t, dir) != 13 {
			t.Fatalf("seed %d: the second open changed the store", seed)
		}
	}
}
