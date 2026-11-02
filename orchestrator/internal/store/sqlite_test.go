package store_test

import (
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

func TestScreenQueriesNeverScan(t *testing.T) {
	s := openSQLite(t, localDir(t), store.SQLiteOptions{})
	plans, err := s.Explain()
	if err != nil {
		t.Fatal(err)
	}
	if len(plans) < 10 {
		t.Fatalf("explained only %d queries: %v", len(plans), plans)
	}
	for name, lines := range plans {
		searched := false
		for _, line := range lines {
			if strings.Contains(line, "SCAN") && !strings.Contains(line, "SCAN CONSTANT ROW") {
				t.Errorf("%s scans: %q\nplan: %v", name, line, lines)
			}
			searched = searched || strings.Contains(line, "SEARCH")
		}
		if !searched && name != "next due" {
			t.Errorf("%s does not search an index: %v", name, lines)
		}
	}
	for name, lines := range plans {
		t.Logf("%-18s %v", name, lines)
	}
}
