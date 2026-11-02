package store_test

import (
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs/localfstest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store/storetest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// populated commits a seeded workload to a fresh file and returns it closed, ready to be damaged.
func populated(t *testing.T, seed uint64, commits int) (localfs.Dir, *storetest.Workload) {
	t.Helper()
	dir := localDir(t)
	s := openSQLite(t, dir, store.SQLiteOptions{})
	w := storetest.NewWorkload(seed)
	for range commits {
		if _, err := s.Commit(w.Next()); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.Verify(); err != nil {
		t.Fatalf("the undamaged store does not verify: %v", err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	return dir, w
}

// damage runs statements on the closed file. Each must change exactly one row.
func damage(t *testing.T, dir localfs.Dir, statements ...string) {
	t.Helper()
	raw, err := sql.Open("sqlite", filepath.Join(dir.Path(), store.DatabaseFile))
	if err != nil {
		t.Fatal(err)
	}
	defer raw.Close()
	for _, statement := range statements {
		res, err := raw.Exec(statement)
		if err != nil {
			t.Fatalf("%s: %v", statement, err)
		}
		if n, _ := res.RowsAffected(); n != 1 {
			t.Fatalf("%s changed %d rows, want 1: the workload lacks a row to damage", statement, n)
		}
	}
}

func requireCorrupt(t *testing.T, dir localfs.Dir, want string) {
	t.Helper()
	s := openSQLite(t, dir, store.SQLiteOptions{})
	err := s.Verify()
	var corrupt store.Corrupt
	if !errors.As(err, &corrupt) {
		t.Fatalf("Verify = %v, want a Corrupt error", err)
	}
	if want != "" && !strings.Contains(err.Error(), want) {
		t.Fatalf("Verify = %v, want it to say %q", err, want)
	}
}

// TestVerifyCatchesDriftInAnyColumnCopiedOutOfABody damages, one at a time, each column that
// duplicates a field of the body (the status, run, parent, delivery and the rest that the indexes
// and the screens' queries use) and each key, and requires Verify to name the damage. The body is
// the truth, so a column that disagrees with it sends a query to the wrong rows.
func TestVerifyCatchesDriftInAnyColumnCopiedOutOfABody(t *testing.T) {
	first := func(table, keys string) string {
		return fmt.Sprintf("(%s) = (SELECT %s FROM %s LIMIT 1)", keys, keys, table)
	}
	cases := []struct{ name, statement string }{
		{"run status", `UPDATE runs SET status = 'zzz' WHERE ` + first("runs", "id")},
		{"run id", `UPDATE runs SET id = 'zzz' WHERE ` + first("runs", "id")},
		{"agent run", `UPDATE agents SET run_id = 'zzz' WHERE ` + first("agents", "id")},
		{"agent parent", `UPDATE agents SET parent_id = 'zzz' WHERE ` + first("agents", "id")},
		{"agent status", `UPDATE agents SET status = 'zzz' WHERE ` + first("agents", "id")},
		{"turn id", `UPDATE turns SET id = 'zzz' WHERE ` + first("turns", "agent_id, ordinal")},
		{"turn run", `UPDATE turns SET run_id = 'zzz' WHERE ` + first("turns", "agent_id, ordinal")},
		{"turn started", `UPDATE turns SET started_at = started_at + 1 WHERE ` + first("turns", "agent_id, ordinal")},
		{"turn ended", `UPDATE turns SET ended_at = COALESCE(ended_at, 0) + 1 WHERE ` + first("turns", "agent_id, ordinal")},
		{"turn ordinal", `UPDATE turns SET ordinal = ordinal + 100000 WHERE ` + first("turns", "agent_id, ordinal")},
		{"item id", `UPDATE items SET id = 'zzz' WHERE ` + first("items", "agent_id, ordinal")},
		{"item turn", `UPDATE items SET turn_id = 'zzz' WHERE ` + first("items", "agent_id, ordinal")},
		{"item ordinal", `UPDATE items SET ordinal = ordinal + 100000 WHERE ` + first("items", "agent_id, ordinal")},
		{"message run", `UPDATE messages SET run_id = 'zzz' WHERE ` + first("messages", "id")},
		{"message to", `UPDATE messages SET to_agent_id = 'zzz' WHERE ` + first("messages", "id")},
		{"message delivery", `UPDATE messages SET delivery = 'zzz' WHERE ` + first("messages", "id")},
		{"run sequence", `UPDATE runs SET updated_sequence = updated_sequence + 1 WHERE ` + first("runs", "id")},
		{"agent sequence", `UPDATE agents SET updated_sequence = updated_sequence + 1 WHERE ` + first("agents", "id")},
		{"turn sequence", `UPDATE turns SET updated_sequence = updated_sequence + 1 WHERE ` + first("turns", "agent_id, ordinal")},
		{"item sequence", `UPDATE items SET updated_sequence = updated_sequence + 1 WHERE ` + first("items", "agent_id, ordinal")},
		{"message sequence", `UPDATE messages SET updated_sequence = updated_sequence + 1 WHERE ` + first("messages", "id")},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			dir, _ := populated(t, 4, 150)
			damage(t, dir, c.statement)
			requireCorrupt(t, dir, "")
		})
	}
}

// TestVerifyCatchesARowNoEventWrites covers the other direction: a row in a table that nothing in
// the log produced. One is a row inserted by hand, one is a row whose event was lost from the log,
// and one is a row of a run that Compact pruned only up to an earlier sequence than the row's.
func TestVerifyCatchesARowNoEventWrites(t *testing.T) {
	at := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	t.Run("an inserted item", func(t *testing.T) {
		dir, _ := populated(t, 4, 60)
		ghost := core.Event{Run: "run-ghost", At: at, Body: core.Item{
			ID: "ghost-item", Turn: "ghost-turn", Agent: "ghost-agent", Ordinal: 1, Status: core.ItemFinished, At: at, Body: core.Assistant{Text: "x"},
		}}
		body, err := wire.EncodeEvent(3, ghost)
		if err != nil {
			t.Fatal(err)
		}
		raw, err := sql.Open("sqlite", filepath.Join(dir.Path(), store.DatabaseFile))
		if err != nil {
			t.Fatal(err)
		}
		_, err = raw.Exec(`INSERT INTO items(agent_id, ordinal, id, turn_id, updated_sequence, body) VALUES ('ghost-agent', 1, 'ghost-item', 'ghost-turn', 3, ?)`, body)
		raw.Close()
		if err != nil {
			t.Fatal(err)
		}
		requireCorrupt(t, dir, "no event")
	})
	t.Run("an event lost from the log", func(t *testing.T) {
		dir, _ := populated(t, 4, 60)
		// Checkpoint at the end of the log, so that a missing event below it is no gap, and the only
		// thing wrong is a row whose event is gone.
		s := openSQLite(t, dir, store.SQLiteOptions{})
		if _, err := s.Compact(store.Retention{}, at); err != nil {
			t.Fatal(err)
		}
		s.Close()
		damage(t, dir, `DELETE FROM events WHERE sequence = (SELECT updated_sequence FROM items LIMIT 1)`)
		requireCorrupt(t, dir, "")
	})
	t.Run("a pruned mark below the row", func(t *testing.T) {
		dir, w := populated(t, 4, 150)
		s := openSQLite(t, dir, store.SQLiteOptions{})
		c, err := s.Compact(store.Retention{}, w.Now().Add(100*24*time.Hour))
		if err != nil || c.EventsDropped == 0 {
			t.Fatalf("the workload has no finished run to prune: %+v %v", c, err)
		}
		s.Close()
		damage(t, dir, `UPDATE pruned SET through = 0 WHERE run_id = (SELECT run_id FROM pruned WHERE run_id != '' LIMIT 1)`)
		requireCorrupt(t, dir, "")
	})
}

// TestRetentionKeepsTheRowsOfAFinishedRun pins what Retention covers: the events go and the tables
// stay, and Verify accepts the rows that outlive their events.
func TestRetentionKeepsTheRowsOfAFinishedRun(t *testing.T) {
	dir, w := populated(t, 4, 300)
	s := openSQLite(t, dir, store.SQLiteOptions{})
	before, err := s.Snapshot()
	if err != nil {
		t.Fatal(err)
	}
	var finished []core.Run
	for _, r := range before.Runs {
		if r.Status.Final() {
			finished = append(finished, r)
		}
	}
	itemsOf := map[core.AgentID]int{}
	for _, a := range before.Agents {
		page, err := s.Items(a.ID, 0, store.MaxPageItems)
		if err != nil {
			t.Fatal(err)
		}
		itemsOf[a.ID] = len(page.Items)
	}
	c, err := s.Compact(store.Retention{}, w.Now().Add(100*24*time.Hour))
	if err != nil || c.EventsDropped == 0 || len(finished) == 0 {
		t.Fatalf("the workload finished %d runs and Compact dropped %d events (%v): nothing to check", len(finished), c.EventsDropped, err)
	}
	if err := s.Verify(); err != nil {
		t.Fatalf("Verify rejects the rows that outlive their events: %v", err)
	}
	for _, r := range finished {
		if _, err := s.EventsAfter(r.ID, 0, 10); !errors.Is(err, store.ErrCompacted) {
			t.Errorf("run %s: its events were dropped, EventsAfter = %v", r.ID, err)
		}
	}
	for _, a := range before.Agents {
		if page, err := s.Items(a.ID, 0, store.MaxPageItems); err != nil || len(page.Items) != itemsOf[a.ID] {
			t.Errorf("agent %s: Compact changed its items from %d to %d (%v)", a.ID, itemsOf[a.ID], len(page.Items), err)
		}
	}
	after, err := s.Snapshot()
	if err != nil || len(after.Runs) != len(before.Runs) {
		t.Errorf("Compact changed the runs a snapshot lists: %d to %d (%v)", len(before.Runs), len(after.Runs), err)
	}
}

// TestVerifyRunsBesideTheWriter calls Verify from inside a commit, while the writer is held at a
// statement. If Verify needed the writer it would never return; the wait is only a guard against
// that hang.
func TestVerifyRunsBesideTheWriter(t *testing.T) {
	var s *store.SQLite
	var verifyErr error
	var done chan struct{}
	armed := false
	s = openSQLite(t, localDir(t), store.SQLiteOptions{Fault: func(op store.Op) error {
		if armed && op.Kind == store.OpExec && done == nil {
			done = make(chan struct{})
			go func() {
				defer close(done)
				verifyErr = s.Verify()
			}()
			select {
			case <-done:
			case <-time.After(20 * time.Second):
				return errors.New("Verify did not finish while a commit held the writer")
			}
		}
		return nil
	}})
	w := storetest.NewWorkload(8)
	for range 20 {
		if _, err := s.Commit(w.Next()); err != nil {
			t.Fatal(err)
		}
	}
	armed = true
	if _, err := s.Commit(w.Next()); err != nil {
		t.Fatal(err)
	}
	if done == nil || verifyErr != nil {
		t.Fatalf("Verify inside the commit: ran=%v err=%v", done != nil, verifyErr)
	}
}

// TestCompactHoldsTheWriterForOneStepAtATime counts the statements of each transaction Compact makes.
// None may hold more than StepRows rows, however large the state, and a state larger than a step
// takes several transactions: that is what bounds the pause a commit can see.
func TestCompactHoldsTheWriterForOneStepAtATime(t *testing.T) {
	const step = 7
	inTx, longest, transactions := 0, 0, 0
	armed := false
	s := openSQLite(t, localDir(t), store.SQLiteOptions{StepRows: step, Fault: func(op store.Op) error {
		if !armed {
			return nil
		}
		switch op.Kind {
		case store.OpExec:
			inTx++
		case store.OpCommit:
			longest, inTx, transactions = max(longest, inTx), 0, transactions+1
		}
		return nil
	}})
	w := storetest.NewWorkload(9)
	for range 200 {
		if _, err := s.Commit(w.Next()); err != nil {
			t.Fatal(err)
		}
	}
	armed = true
	c, err := s.Compact(store.Retention{}, w.Now().Add(100*24*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if longest > step {
		t.Errorf("a Compact transaction wrote %d rows, over the step of %d", longest, step)
	}
	if transactions < 10 || c.EventsDropped == 0 {
		t.Errorf("Compact took %d transactions and dropped %d events: too small to show steps", transactions, c.EventsDropped)
	}
}

// TestACrashDuringCompactLeavesTheSameState visits every write step of a Compact, with steps small
// enough to make many, copies the files at each, and requires the copy to recover the state of the
// whole log, verify, and finish the Compact.
func TestACrashDuringCompactLeavesTheSameState(t *testing.T) {
	dir := localDir(t)
	var copies []string
	armed := false
	s := openSQLite(t, dir, store.SQLiteOptions{StepRows: 12, Fault: func(store.Op) error {
		if !armed {
			return nil
		}
		copyPath := t.TempDir()
		for _, name := range []string{store.DatabaseFile, store.DatabaseFile + "-wal"} {
			b, err := os.ReadFile(filepath.Join(dir.Path(), name))
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			if err != nil {
				t.Error(err)
				continue
			}
			if err := os.WriteFile(filepath.Join(copyPath, name), b, 0o600); err != nil {
				t.Error(err)
			}
		}
		copies = append(copies, copyPath)
		return nil
	}})
	w := storetest.NewWorkload(12)
	var events []core.Event
	for range 70 {
		r := w.Next()
		if _, err := s.Commit(r); err != nil {
			t.Fatal(err)
		}
		events = append(events, r.Events...)
	}
	armed = true
	// Two rounds: the first writes the first checkpoint and prunes, the second replaces it.
	retention := store.Retention{}
	later := w.Now().Add(100 * 24 * time.Hour)
	if _, err := s.Compact(retention, later); err != nil {
		t.Fatal(err)
	}
	for range 10 {
		r := w.Next()
		events = append(events, r.Events...)
		armed = false
		if _, err := s.Commit(r); err != nil {
			t.Fatal(err)
		}
		armed = true
	}
	if _, err := s.Compact(retention, later); err != nil {
		t.Fatal(err)
	}
	if len(copies) < 30 {
		t.Fatalf("only %d write steps in two Compacts", len(copies))
	}
	for i, path := range copies {
		d, err := localfs.Resolve(path, localfstest.Fake{})
		if err != nil {
			t.Fatal(err)
		}
		t.Run(fmt.Sprintf("step-%d", i+1), func(t *testing.T) {
			c := openSQLite(t, d, store.SQLiteOptions{})
			state, err := c.Recover()
			if err != nil {
				t.Fatal(err)
			}
			// A copy taken before the ten later commits lacks them, so compare against the events it
			// holds: the state it recovers must be a prefix state, and its log must verify.
			snap, err := c.Snapshot()
			if err != nil {
				t.Fatal(err)
			}
			upTo := core.Replay(core.State{}, roundTrip(t, events[:snap.Sequence]))
			if !state.Equal(upTo) {
				t.Fatalf("the recovered state is not the state of the first %d events", snap.Sequence)
			}
			if err := c.Verify(); err != nil {
				t.Fatal(err)
			}
			if _, err := c.Compact(retention, later); err != nil {
				t.Fatal(err)
			}
			if again, err := c.Recover(); err != nil || !again.Equal(upTo) {
				t.Fatalf("finishing the Compact changed the state: %v", err)
			}
			if err := c.Verify(); err != nil {
				t.Fatal(err)
			}
		})
	}
}

// TestCommitsAndCompactionsAndVerifiesInterleave runs a writer beside a loop of Compact and Verify.
// The outcome does not depend on how they interleave: every commit lands, the store verifies, and
// it recovers the state of everything committed. Under the race detector it also checks the locking.
func TestCommitsAndCompactionsAndVerifiesInterleave(t *testing.T) {
	s := openSQLite(t, localDir(t), store.SQLiteOptions{StepRows: 50})
	w := storetest.NewWorkload(13)
	results := make([]store.Result, 250)
	for i := range results {
		results[i] = w.Next()
	}
	var wg sync.WaitGroup
	done := make(chan struct{})
	errs := make(chan error, 1)
	wg.Add(1)
	go func() {
		defer wg.Done()
		defer close(done)
		for _, r := range results {
			if _, err := s.Commit(r); err != nil {
				errs <- err
				return
			}
		}
	}()
	later := w.Now().Add(100 * 24 * time.Hour)
	for running := true; running; {
		select {
		case <-done:
			running = false
		default:
			if _, err := s.Compact(store.Retention{CheckpointAfter: 20}, later); err != nil {
				t.Fatal(err)
			}
			if err := s.Verify(); err != nil {
				t.Fatal(err)
			}
		}
	}
	wg.Wait()
	select {
	case err := <-errs:
		t.Fatal(err)
	default:
	}
	var events []core.Event
	for _, r := range results {
		events = append(events, r.Events...)
	}
	state, err := s.Recover()
	if err != nil || !state.Equal(core.Replay(core.State{}, roundTrip(t, events))) {
		t.Fatalf("the store did not recover everything committed: %v", err)
	}
	if err := s.Verify(); err != nil {
		t.Fatal(err)
	}
}
