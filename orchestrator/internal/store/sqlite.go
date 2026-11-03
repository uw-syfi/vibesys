package store

import (
	"cmp"
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"sync/atomic"
	"time"

	"modernc.org/sqlite" // A pure Go driver, so the daemon cross-compiles with no C toolchain.

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// SQLiteOptions tune a SQLite store. The zero value is what the daemon uses.
type SQLiteOptions struct {
	// Fault, when set, is called at every step of every write the store makes to its file: before
	// each statement, before and after each COMMIT (see Op). An error from it fails the step, and a
	// hook that ends the process is a crash at that step. It exists for tests, and it sees the
	// writes as the database driver does, so it covers whatever transactions the store uses.
	Fault func(Op) error
	// StepRows is how many rows one step of Compact writes before it lets the writer go. Zero means
	// DefaultStepRows. Tests shrink it to see many steps in a small store.
	StepRows int
}

// DefaultStepRows is how many rows a step of Compact writes: a checkpoint batch, or events dropped.
// At a few microseconds a row it holds the writer for a few milliseconds.
const DefaultStepRows = 1000

// SQLite is a Store in one SQLite file: write-ahead log, synchronous NORMAL, one writer. Reads, Verify
// and the long part of Compact run on snapshots of the file, beside the writer, which they never
// hold for more than a short step.
//
// NORMAL means a power cut may lose the last commits, but never leaves a file that is not a prefix
// of what was committed: the daemon's recovery relies on exactly that, and the crash tests check
// it at every statement and COMMIT of a commit. A process crash loses nothing that Commit returned.
type SQLite struct {
	path    string
	fault   func(Op) error
	release func()

	mu   sync.Mutex // the writer: every write to the file takes it, and no read does
	w    *sql.DB
	r    *sql.DB
	last uint64
	// closed is read without the writer lock, so that a read never waits for a write.
	closed atomic.Bool
	// step is how many rows one step of a long write holds the writer for.
	step int

	// compacting serializes Compact calls, which write in several short steps with the writer free
	// between them.
	compacting sync.Mutex
}

// readerConns is how many connections the readers may hold at once.
const readerConns = 4

// DatabaseFile is the name of the file in a state directory.
const DatabaseFile = "orchestrator.db"

// OpenSQLite opens the store in a directory proven local. It creates what is missing, refuses a file
// of another schema version and one already open in another process, and returns each effect that
// was claimed when the last process died to the outbox.
func OpenSQLite(dir localfs.Dir, opts SQLiteOptions) (*SQLite, error) {
	if dir.Path() == "" {
		return nil, errors.New("store: no state directory: resolve one with localfs first")
	}
	if err := os.MkdirAll(dir.Path(), 0o700); err != nil {
		return nil, fmt.Errorf("store: %w", err)
	}
	release, err := lockDir(dir.Path())
	if err != nil {
		return nil, err
	}
	s := &SQLite{path: filepath.Join(dir.Path(), DatabaseFile), fault: opts.Fault, release: release, step: cmp.Or(opts.StepRows, DefaultStepRows)}
	if err := s.open(); err != nil {
		s.close()
		return nil, err
	}
	return s, nil
}

func (s *SQLite) dsn(readOnly bool) string {
	q := url.Values{}
	for _, p := range []string{"journal_mode(WAL)", "synchronous(NORMAL)", "busy_timeout(10000)", "foreign_keys(OFF)"} {
		q.Add("_pragma", p)
	}
	if readOnly {
		q.Set("mode", "ro")
		q.Add("_pragma", "query_only(1)")
	} else {
		q.Set("_txlock", "immediate")
	}
	u := url.URL{Scheme: "file", Path: s.path, RawQuery: q.Encode()}
	return u.String()
}

func (s *SQLite) open() (err error) {
	base, err := sqlite.NewConnector(s.dsn(false))
	if err != nil {
		return fmt.Errorf("store: %w", err)
	}
	if s.fault != nil {
		base = faultConnector{Connector: base, fault: s.fault}
	}
	s.w = sql.OpenDB(base)
	s.w.SetMaxOpenConns(1)
	if _, err := s.w.Exec(schema); err != nil {
		return fmt.Errorf("store: create schema in %s: %w", s.path, err)
	}
	if _, err := s.w.Exec(`INSERT OR IGNORE INTO meta(key, value) VALUES
		('schema_version', ?), ('last_sequence', 0), ('checkpoint_sequence', 0), ('checkpoint_gen', 0), ('pruned_max', 0)`, schemaVersion); err != nil {
		return fmt.Errorf("store: %w", err)
	}
	if v, err := s.meta(s.w, "schema_version"); err != nil || (v != 2 && v != schemaVersion) {
		return fmt.Errorf("store: %s has schema version %d, this build reads %d (%v)", s.path, v, schemaVersion, err)
	}
	if s.r, err = sql.Open("sqlite", s.dsn(true)); err != nil {
		return fmt.Errorf("store: %w", err)
	}
	s.r.SetMaxOpenConns(readerConns)
	if _, err := s.w.Exec(`UPDATE outbox SET state = 0 WHERE state = 1`); err != nil {
		return fmt.Errorf("store: %w", err)
	}
	if s.last, err = s.meta(s.w, "last_sequence"); err != nil {
		return err
	}
	var max sql.NullInt64
	if err := s.w.QueryRow(`SELECT MAX(sequence) FROM events`).Scan(&max); err != nil {
		return fmt.Errorf("store: %w", err)
	}
	if uint64(max.Int64) > s.last {
		return corruptf("the log holds event %d and the store says it ends at %d", max.Int64, s.last)
	}
	ckpt, err := s.meta(s.w, "checkpoint_sequence")
	if err != nil {
		return err
	}
	if ckpt > s.last {
		return corruptf("the checkpoint is at %d, past the end of the log at %d", ckpt, s.last)
	}
	if v, _ := s.meta(s.w, "schema_version"); v == 2 {
		return s.upgradeIdentity()
	}
	return nil
}

type queryRower interface {
	QueryRow(query string, args ...any) *sql.Row
}

func (s *SQLite) meta(q queryRower, key string) (uint64, error) {
	var v int64
	if err := q.QueryRow(`SELECT value FROM meta WHERE key = ?`, key).Scan(&v); err != nil {
		return 0, fmt.Errorf("store: meta %s: %w", key, err)
	}
	return uint64(v), nil
}

// Close implements Store.
func (s *SQLite) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.close()
}

func (s *SQLite) close() error {
	if s.closed.Swap(true) {
		return nil
	}
	var errs []error
	if s.r != nil {
		errs = append(errs, s.r.Close())
	}
	if s.w != nil {
		errs = append(errs, s.w.Close())
	}
	if s.release != nil {
		s.release()
	}
	return errors.Join(errs...)
}

// Commit implements Store.
func (s *SQLite) Commit(r Result) (Committed, error) {
	if len(r.Events) == 0 && len(r.Effects) == 0 {
		return Committed{}, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed.Load() {
		return Committed{}, errClosed
	}
	first := s.last + 1
	events, effects, err := encodeResult(first, r)
	if err != nil {
		return Committed{}, err
	}
	// Whatever is stored must read back, or a restart would be the first to find out.
	for i, b := range events {
		if _, _, err := wire.DecodeEvent(b); err != nil {
			return Committed{}, fmt.Errorf("store: event %d does not read back: %w", i+1, err)
		}
	}
	for i, b := range effects {
		if _, err := wire.DecodeEffect(b); err != nil {
			return Committed{}, fmt.Errorf("store: effect %d does not read back: %w", i+1, err)
		}
	}
	tx, err := s.w.Begin()
	if err != nil {
		return Committed{}, fmt.Errorf("store: begin: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // After Commit it reports that it has nothing to undo, which is the point.
	for i, b := range events {
		e := r.Events[i]
		if _, err := tx.Exec(`INSERT INTO events(sequence, run_id, command_id, at, body) VALUES (?, ?, ?, ?, ?)`,
			first+uint64(i), string(e.Run), string(e.Command), e.At.UnixNano(), b); err != nil {
			return Committed{}, fmt.Errorf("store: write event %d: %w", first+uint64(i), err)
		}
	}
	for i, b := range events {
		if err := project(tx, first+uint64(i), r.Events[i], b); err != nil {
			return Committed{}, err
		}
	}
	for i, b := range effects {
		after, _ := runAfter(r.Effects[i])
		if _, err := tx.Exec(`INSERT INTO outbox(effect_id, effect, state, run_after) VALUES (?, ?, 0, ?)`,
			string(r.Effects[i].ID()), b, after); err != nil {
			return Committed{}, fmt.Errorf("store: queue effect %s: %w", r.Effects[i].ID(), err)
		}
	}
	last := first + uint64(len(events)) - 1
	if len(events) > 0 {
		if _, err := tx.Exec(`UPDATE meta SET value = ? WHERE key = 'last_sequence'`, last); err != nil {
			return Committed{}, fmt.Errorf("store: %w", err)
		}
	}
	if err := tx.Commit(); err != nil {
		// The error may be a reply lost after the file took the commit, so the file says where the log
		// ends. Keeping the old end would make the next commit reuse a sequence.
		if end, merr := s.meta(s.w, "last_sequence"); merr == nil {
			s.last = end
		}
		return Committed{}, fmt.Errorf("store: commit: %w", err)
	}
	s.last = last
	return Committed{First: first, Last: last}, nil
}

// decodeRows reads the single body column of rows into events.
func decodeBody(body []byte) (uint64, core.Event, error) {
	seq, e, err := wire.DecodeEvent(body)
	if err != nil {
		return 0, core.Event{}, corruptf("a stored event does not decode: %v", err)
	}
	return seq, e, nil
}

// entity takes the body of a stored event as the kind its table holds. A row whose bytes decode
// into another kind is corruption, and is reported as such.
func entity[T core.EventBody](e core.Event) (T, error) {
	v, ok := e.Body.(T)
	if !ok {
		return v, corruptf("a stored row holds a %T where a %T belongs", e.Body, v)
	}
	return v, nil
}

// batchSize bounds how many decoded events wait for one Replay, which copies the state's maps once
// per call.
const batchSize = 4096

// live reports errClosed once the store is closed. It takes no lock: a read must not wait for the writer.
func (s *SQLite) live() error {
	if s.closed.Load() {
		return errClosed
	}
	return nil
}

// write runs f in one transaction on the writer, holding the writer for just that long.
func (s *SQLite) write(f func(tx *sql.Tx) error) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed.Load() {
		return errClosed
	}
	tx, err := s.w.Begin()
	if err != nil {
		return fmt.Errorf("store: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // After Commit it has nothing to undo.
	if err := f(tx); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("store: %w", err)
	}
	return nil
}

// Recover implements Store. It reads a snapshot and does not wait for, or hold up, the writer.
func (s *SQLite) Recover() (core.State, error) {
	if err := s.live(); err != nil {
		return core.State{}, err
	}
	var state core.State
	err := s.reading(func(tx *sql.Tx) error {
		var err error
		state, _, err = recoverFrom(tx)
		return err
	})
	return state, err
}

// recoverFrom folds the checkpoint in force and the events after it, as one read transaction sees
// them, and returns the state with the sequence it is at.
func recoverFrom(tx *sql.Tx) (core.State, uint64, error) {
	var state core.State
	m := func(key string) (uint64, error) {
		var v int64
		if err := tx.QueryRow(`SELECT value FROM meta WHERE key = ?`, key).Scan(&v); err != nil {
			return 0, fmt.Errorf("store: meta %s: %w", key, err)
		}
		return uint64(v), nil
	}
	ckpt, err := m("checkpoint_sequence")
	if err != nil {
		return state, 0, err
	}
	gen, err := m("checkpoint_gen")
	if err != nil {
		return state, 0, err
	}
	last, err := m("last_sequence")
	if err != nil {
		return state, 0, err
	}
	var batch []core.Event
	flush := func() {
		state = core.Replay(state, batch)
		batch = batch[:0]
	}
	read := func(query string, args ...any) error {
		rows, err := tx.Query(query, args...)
		if err != nil {
			return fmt.Errorf("store: %w", err)
		}
		defer rows.Close()
		for rows.Next() {
			var body []byte
			if err := rows.Scan(&body); err != nil {
				return fmt.Errorf("store: %w", err)
			}
			_, e, err := decodeBody(body)
			if err != nil {
				return err
			}
			if batch = append(batch, e); len(batch) == batchSize {
				flush()
			}
		}
		flush()
		return rows.Err()
	}
	if err := read(queryCheckpoint, gen); err != nil {
		return state, 0, err
	}
	if err := read(`SELECT body FROM events WHERE sequence > ? ORDER BY sequence`, ckpt); err != nil {
		return state, 0, err
	}
	return state, last, nil
}

// Claim implements Store.
func (s *SQLite) Claim(now time.Time, limit int) ([]Claimed, error) {
	var out []Claimed
	err := s.write(func(tx *sql.Tx) error {
		rows, err := tx.Query(queryClaim, now.UnixNano(), limit)
		if err != nil {
			return fmt.Errorf("store: %w", err)
		}
		for rows.Next() {
			var id int64
			var b []byte
			if err := rows.Scan(&id, &b); err != nil {
				rows.Close()
				return fmt.Errorf("store: %w", err)
			}
			e, err := wire.DecodeEffect(b)
			if err != nil {
				rows.Close()
				return corruptf("outbox row %d: %v", id, err)
			}
			out = append(out, Claimed{ID: OutboxID(id), Effect: e})
		}
		if err := rows.Close(); err != nil {
			return fmt.Errorf("store: %w", err)
		}
		for _, c := range out {
			if _, err := tx.Exec(`UPDATE outbox SET state = 1 WHERE id = ?`, c.ID); err != nil {
				return fmt.Errorf("store: %w", err)
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

// Complete implements Store.
func (s *SQLite) Complete(ids ...OutboxID) error {
	return s.settle(`DELETE FROM outbox WHERE state = 1 AND id = ?`, ids)
}

// Release implements Store.
func (s *SQLite) Release(ids ...OutboxID) error {
	return s.settle(`UPDATE outbox SET state = 0 WHERE state = 1 AND id = ?`, ids)
}

func (s *SQLite) settle(statement string, ids []OutboxID) error {
	return s.write(func(tx *sql.Tx) error {
		for _, id := range ids {
			if _, err := tx.Exec(statement, id); err != nil {
				return fmt.Errorf("store: %w", err)
			}
		}
		return nil
	})
}

// Reclaim implements Store.
func (s *SQLite) Reclaim() (int, error) {
	n := 0
	err := s.write(func(tx *sql.Tx) error {
		res, err := tx.Exec(`UPDATE outbox SET state = 0 WHERE state = 1`)
		if err != nil {
			return fmt.Errorf("store: %w", err)
		}
		c, _ := res.RowsAffected()
		n = int(c)
		return nil
	})
	return n, err
}

// NextDue implements Store.
func (s *SQLite) NextDue() (time.Time, bool, error) {
	var nanos sql.NullInt64
	if err := s.r.QueryRow(queryNextDue).Scan(&nanos); err != nil {
		return time.Time{}, false, fmt.Errorf("store: %w", err)
	}
	return time.Unix(0, nanos.Int64).UTC(), nanos.Valid, nil
}

// Compact implements Store. The work is done on a snapshot, beside the writer, and the file is
// written in steps of at most StepRows rows, each of which holds the writer for
// its own length only. A crash between steps leaves a store that is consistent and recovers the same
// state; the next Compact finishes the work.
func (s *SQLite) Compact(p Retention, now time.Time) (Compaction, error) {
	s.compacting.Lock()
	defer s.compacting.Unlock()
	if err := s.live(); err != nil {
		return Compaction{}, err
	}
	var out Compaction
	var state core.State
	var seq, ckpt, gen uint64
	var due bool
	err := s.reading(func(tx *sql.Tx) error {
		var err error
		if ckpt, err = s.meta(tx, "checkpoint_sequence"); err != nil {
			return err
		}
		if gen, err = s.meta(tx, "checkpoint_gen"); err != nil {
			return err
		}
		if seq, err = s.meta(tx, "last_sequence"); err != nil {
			return err
		}
		if due = seq > ckpt && seq-ckpt >= p.CheckpointAfter; due {
			state, _, err = recoverFrom(tx)
		}
		return err
	})
	if err != nil {
		return Compaction{}, err
	}
	if due {
		if err := s.writeCheckpoint(state, seq, gen+1); err != nil {
			return Compaction{}, err
		}
		ckpt, gen, out.Checkpointed = seq, gen+1, true
	}
	out.Checkpoint = ckpt
	if err := s.dropCheckpoints(`gen < ?`, gen); err != nil {
		return Compaction{}, err
	}
	dropped, err := s.prune(p, now, ckpt)
	if err != nil {
		return Compaction{}, err
	}
	out.EventsDropped = dropped
	return out, nil
}

// writeCheckpoint writes the state as generation gen in batches, and then switches to it in one
// write that touches two meta rows. Until the switch the generation in force is unchanged, so a
// reader or a crash never sees a half-written checkpoint.
func (s *SQLite) writeCheckpoint(state core.State, seq, gen uint64) error {
	if err := s.dropCheckpoints(`gen >= ?`, gen); err != nil { // the leftovers of a Compact that died
		return err
	}
	events := state.Checkpoint()
	for from := 0; from < len(events); from += s.step {
		batch := events[from:min(from+s.step, len(events))]
		err := s.write(func(tx *sql.Tx) error {
			for i, e := range batch {
				b, err := wire.EncodeEvent(seq, e)
				if err != nil {
					return fmt.Errorf("store: encode checkpoint event %d: %w", from+i, err)
				}
				if _, _, err := wire.DecodeEvent(b); err != nil {
					return fmt.Errorf("store: checkpoint event %d does not read back: %w", from+i, err)
				}
				if _, err := tx.Exec(`INSERT INTO checkpoint(gen, idx, body) VALUES (?, ?, ?)`, gen, from+i+1, b); err != nil {
					return fmt.Errorf("store: %w", err)
				}
			}
			return nil
		})
		if err != nil {
			return err
		}
	}
	return s.write(func(tx *sql.Tx) error {
		for key, value := range map[string]uint64{"checkpoint_sequence": seq, "checkpoint_gen": gen} {
			if _, err := tx.Exec(`UPDATE meta SET value = ? WHERE key = ?`, value, key); err != nil {
				return fmt.Errorf("store: %w", err)
			}
		}
		return nil
	})
}

// dropCheckpoints deletes the checkpoint rows that match a condition on gen, a batch at a time.
func (s *SQLite) dropCheckpoints(cond string, gen uint64) error {
	for {
		var n int64
		err := s.write(func(tx *sql.Tx) error {
			res, err := tx.Exec(`DELETE FROM checkpoint WHERE (gen, idx) IN (SELECT gen, idx FROM checkpoint WHERE `+cond+` LIMIT ?)`, gen, s.step)
			if err != nil {
				return fmt.Errorf("store: %w", err)
			}
			n, _ = res.RowsAffected()
			return nil
		})
		if err != nil || n < int64(s.step) {
			return err
		}
	}
}

// prune drops the events a checkpoint covers whose run is over and old enough, and records how far
// it went for each run, so a reader asking for them is told they are gone. The tables keep the
// entities: see Retention.
//
// Which runs and how far is decided on a snapshot. For each run the mark in pruned is written
// first, then the events go in batches, so a reader is told the events are gone as soon as any
// are, and a crash between batches leaves events that the next Compact removes.
func (s *SQLite) prune(p Retention, now time.Time, ckpt uint64) (int, error) {
	type target struct {
		run     core.RunID
		through uint64
	}
	var targets []target
	cutoff := now.Add(-p.KeepEvents).UnixNano()
	err := s.reading(func(tx *sql.Tx) error {
		var eligible []core.RunID
		for _, status := range []core.RunStatus{core.RunEnded, core.RunFailed} {
			rows, err := tx.Query(`SELECT body FROM runs WHERE status = ?`, string(status))
			if err != nil {
				return fmt.Errorf("store: %w", err)
			}
			err = scanEvents(rows, func(_ uint64, e core.Event) error {
				run, err := entity[core.Run](e)
				if err == nil && !run.EndedAt.Add(p.KeepEvents).After(now) {
					eligible = append(eligible, run.ID)
				}
				return err
			})
			if err != nil {
				return err
			}
		}
		eligible = append(eligible, "") // commands refused before any run, aged by their own time
		slices.Sort(eligible)
		for _, run := range eligible {
			var through sql.NullInt64
			var err error
			if run == "" {
				err = tx.QueryRow(`SELECT MAX(sequence) FROM events WHERE run_id = '' AND sequence <= ? AND at <= ?`, ckpt, cutoff).Scan(&through)
			} else {
				err = tx.QueryRow(`SELECT MAX(sequence) FROM events WHERE run_id = ? AND sequence <= ?`, string(run), ckpt).Scan(&through)
			}
			if err != nil {
				return fmt.Errorf("store: %w", err)
			}
			if through.Valid {
				targets = append(targets, target{run, uint64(through.Int64)})
			}
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	total := 0
	for _, t := range targets {
		err := s.write(func(tx *sql.Tx) error {
			if _, err := tx.Exec(`INSERT INTO pruned(run_id, through) VALUES (?, ?)
				ON CONFLICT(run_id) DO UPDATE SET through = MAX(through, excluded.through)`, string(t.run), t.through); err != nil {
				return fmt.Errorf("store: %w", err)
			}
			if _, err := tx.Exec(`UPDATE meta SET value = MAX(value, ?) WHERE key = 'pruned_max'`, t.through); err != nil {
				return fmt.Errorf("store: %w", err)
			}
			return nil
		})
		if err != nil {
			return total, err
		}
		for {
			var n int64
			err := s.write(func(tx *sql.Tx) error {
				// "at" bounds the empty run only; the others are dropped whole, up to the checkpoint.
				res, err := tx.Exec(`DELETE FROM events WHERE sequence IN (
					SELECT sequence FROM events WHERE run_id = ? AND sequence <= ? AND (run_id != '' OR at <= ?) LIMIT ?)`,
					string(t.run), t.through, cutoff, s.step)
				if err != nil {
					return fmt.Errorf("store: %w", err)
				}
				n, _ = res.RowsAffected()
				return nil
			})
			total += int(n)
			if err != nil {
				return total, err
			}
			if n < int64(s.step) {
				break
			}
		}
	}
	return total, nil
}

// Verify implements Store. It reads one snapshot beside the writer, so commits go on while it runs.
func (s *SQLite) Verify() error {
	if err := s.live(); err != nil {
		return err
	}
	return s.reading(func(tx *sql.Tx) error {
		state, last, err := recoverFrom(tx)
		if err != nil {
			return err
		}
		if err := s.verifyLog(tx, last); err != nil {
			return err
		}
		if err := verifySummary(tx, state); err != nil {
			return err
		}
		return verifyProjection(tx)
	})
}

// verifyLog checks the events: ascending, with no gap above the checkpoint, each equal to its
// columns, and the end where the snapshot says.
func (s *SQLite) verifyLog(tx *sql.Tx, last uint64) error {
	ckpt, err := s.meta(tx, "checkpoint_sequence")
	if err != nil {
		return err
	}
	rows, err := tx.Query(`SELECT sequence, run_id, command_id, at, body FROM events ORDER BY sequence`)
	if err != nil {
		return fmt.Errorf("store: %w", err)
	}
	defer rows.Close()
	var prev uint64
	next := ckpt + 1
	for rows.Next() {
		var seq uint64
		var run, command string
		var at int64
		var body []byte
		if err := rows.Scan(&seq, &run, &command, &at, &body); err != nil {
			return fmt.Errorf("store: %w", err)
		}
		if seq <= prev {
			return corruptf("event %d follows %d", seq, prev)
		}
		prev = seq
		if seq > ckpt {
			if seq != next {
				return corruptf("event %d follows %d with a gap", seq, next-1)
			}
			next++
		}
		got, e, err := decodeBody(body)
		switch {
		case err != nil:
			return err
		case got != seq:
			return corruptf("event row %d holds event %d", seq, got)
		case string(e.Run) != run || string(e.Command) != command || e.At.UnixNano() != at:
			return corruptf("event %d disagrees with its columns", seq)
		}
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("store: %w", err)
	}
	if end := max(next-1, ckpt); end != last {
		return corruptf("the log ends at %d and the store says %d", end, last)
	}
	return nil
}

// verifySummary loads the tables a state must equal and compares them.
func verifySummary(tx *sql.Tx, state core.State) error {
	sm := newSummary()
	load := func(query string, each func(core.Event) error, args ...any) error {
		rows, err := tx.Query(query, args...)
		if err != nil {
			return fmt.Errorf("store: %w", err)
		}
		defer rows.Close()
		for rows.Next() {
			var body []byte
			if err := rows.Scan(&body); err != nil {
				return fmt.Errorf("store: %w", err)
			}
			_, e, err := decodeBody(body)
			if err != nil {
				return err
			}
			if err := each(e); err != nil {
				return err
			}
		}
		return rows.Err()
	}
	if err := load(`SELECT body FROM runs`, func(e core.Event) error {
		r, err := entity[core.Run](e)
		sm.runs[r.ID] = r
		return err
	}); err != nil {
		return err
	}
	if err := load(`SELECT body FROM agents`, func(e core.Event) error {
		a, err := entity[core.Agent](e)
		sm.agents[a.ID] = a
		return err
	}); err != nil {
		return err
	}
	if err := load(`SELECT result FROM command_receipts`, func(e core.Event) error {
		r, err := entity[core.Receipt](e)
		sm.receipts[r.Command] = r
		return err
	}); err != nil {
		return err
	}
	if err := load(`SELECT body FROM messages WHERE delivery NOT IN (?, ?)`, func(e core.Event) error {
		m, err := entity[core.Message](e)
		sm.transit[m.ID] = m
		return err
	}, string(core.DeliveryConsumed), string(core.DeliveryRejected)); err != nil {
		return err
	}
	return sm.check(state)
}
