package store_test

import (
	"database/sql"
	"fmt"
	"math/rand/v2"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store/storetest"
)

// TestDamagedBytesNeverPanic damages one stored value in a populated file, by changing a byte,
// cutting it short or swapping in another table's bytes, and reopens it. Whatever the damage, every
// call either answers or returns an error: the store decodes bytes it did not just write, and a
// panic there would take the daemon down at start. The failing seed replays the damage.
func TestDamagedBytesNeverPanic(t *testing.T) {
	tables := []struct{ table, key string }{
		{"events", "sequence"}, {"runs", "id"}, {"agents", "id"}, {"messages", "id"}, {"checkpoint", "idx"},
		{"outbox", "id"}, {"command_receipts", "command_id"},
	}
	column := map[string]string{"command_receipts": "result", "outbox": "effect"}
	for seed := uint64(1); seed <= 40; seed++ {
		t.Run(fmt.Sprint("seed-", seed), func(t *testing.T) {
			r := rand.New(rand.NewPCG(seed, 99))
			dir := localDir(t)
			s := openSQLite(t, dir, store.SQLiteOptions{})
			w := storetest.NewWorkload(seed)
			for range 60 {
				if _, err := s.Commit(w.Next()); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := s.Compact(store.Retention{}, w.Now()); err != nil {
				t.Fatal(err)
			}
			if err := s.Close(); err != nil {
				t.Fatal(err)
			}
			raw, err := sql.Open("sqlite", filepath.Join(dir.Path(), store.DatabaseFile))
			if err != nil {
				t.Fatal(err)
			}
			spec := tables[r.IntN(len(tables))]
			col := "body"
			if c, ok := column[spec.table]; ok {
				col = c
			}
			var key any
			var body []byte
			query := fmt.Sprintf("SELECT %s, %s FROM %s ORDER BY RANDOM() LIMIT 1", spec.key, col, spec.table)
			if err := raw.QueryRow(query).Scan(&key, &body); err != nil {
				t.Skipf("%s is empty in this seed: %v", spec.table, err)
			}
			switch r.IntN(3) {
			case 0:
				body[r.IntN(len(body))] ^= byte(1 + r.IntN(255))
			case 1:
				body = body[:r.IntN(len(body))]
			default:
				var other []byte
				if err := raw.QueryRow("SELECT body FROM items ORDER BY RANDOM() LIMIT 1").Scan(&other); err == nil {
					body = other
				} else {
					body = nil
				}
			}
			if _, err := raw.Exec(fmt.Sprintf("UPDATE %s SET %s = ? WHERE %s = ?", spec.table, col, spec.key), body, key); err != nil {
				t.Fatal(err)
			}
			raw.Close()

			defer func() {
				if p := recover(); p != nil {
					t.Fatalf("seed %d (%s) panicked on damaged bytes: %v\nreplay: go test ./internal/store -run 'TestDamagedBytesNeverPanic/seed-%d$'", seed, spec.table, p, seed)
				}
			}()
			s, err = store.OpenSQLite(dir, store.SQLiteOptions{})
			if err != nil {
				return // Refusing to open is an answer.
			}
			defer s.Close()
			_, _ = s.Recover()
			_ = s.Verify()
			snap, _ := s.Snapshot()
			_, _ = s.EventsAfter("", 0, 0)
			for _, run := range snap.Runs {
				_, _ = s.EventsAfter(run.ID, 0, 0)
				_, _ = s.Messages(run.ID, 0, 0)
				_, _ = s.Turns(run.ID, time.Time{}, farFutureForTests, 0)
			}
			for _, a := range snap.Agents {
				_, _ = s.Items(a.ID, 0, 0)
			}
			_, _ = s.Claim(farFutureForTests, 100)
			_, _ = s.Compact(store.Retention{}, w.Now())
		})
	}
}

var farFutureForTests = time.Date(2200, 1, 1, 0, 0, 0, 0, time.UTC)

// A store of any other schema version is refused with what to do about it, and left as it was: there
// is no upgrade, and a store that is read wrongly is worse than one that is not read.
func TestAStoreOfAnotherSchemaVersionIsRefusedWithWhatToDo(t *testing.T) {
	for _, version := range []int{1, 2, 3, 4, 5, 7, 100} {
		t.Run(fmt.Sprint(version), func(t *testing.T) {
			dir := localDir(t)
			s := openSQLite(t, dir, store.SQLiteOptions{})
			if err := s.Close(); err != nil {
				t.Fatal(err)
			}
			raw, err := sql.Open("sqlite", filepath.Join(dir.Path(), store.DatabaseFile))
			if err != nil {
				t.Fatal(err)
			}
			if _, err := raw.Exec(`UPDATE meta SET value = ? WHERE key = 'schema_version'`, version); err != nil {
				t.Fatal(err)
			}
			if err := raw.Close(); err != nil {
				t.Fatal(err)
			}
			_, err = store.OpenSQLite(dir, store.SQLiteOptions{})
			if err == nil || !strings.Contains(err.Error(), "move the state directory") || !strings.Contains(err.Error(), fmt.Sprint(version)) {
				t.Fatalf("got %v, want a refusal naming the version and what to do", err)
			}
		})
	}
}
