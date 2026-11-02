package store

import (
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"time"

	_ "modernc.org/sqlite" // A pure Go driver, so the daemon cross-compiles with no C toolchain.

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// Point is a place in Commit where a fault can be injected.
type Point string

// The points, in the order Commit passes them. Everything before PointAfterCommit is inside the
// transaction: a failure or a kill there leaves no trace of the result. PointAfterCommit is after
// the transaction, where a failure means the result is durable and its caller was not told.
const (
	PointBegin        Point = "begin"
	PointEvents       Point = "events"
	PointTables       Point = "tables"
	PointOutbox       Point = "outbox"
	PointBeforeCommit Point = "before-commit"
	PointAfterCommit  Point = "after-commit"
)

// Points lists every commit boundary, for tests that visit each.
var Points = []Point{PointBegin, PointEvents, PointTables, PointOutbox, PointBeforeCommit, PointAfterCommit}

// SQLiteOptions tune a SQLite store. The zero value is what the daemon uses.
type SQLiteOptions struct {
	// Fault, when set, is called at each Point of every Commit. An error from it fails the commit
	// there. It exists for tests: a hook that ends the process is a crash at that boundary.
	Fault func(Point) error
}

// SQLite is a Store in one SQLite file: write-ahead log, synchronous NORMAL, one writer.
//
// NORMAL means a power cut may lose the last commits, but never leaves a file that is not a prefix
// of what was committed: the daemon's recovery relies on exactly that, and the crash tests check
// it at every boundary of a commit. A process crash loses nothing that Commit returned.
type SQLite struct {
	path    string
	fault   func(Point) error
	release func()

	mu     sync.Mutex // the writer: Commit, Compact, Claim, Complete, Release, Verify
	w      *sql.DB
	r      *sql.DB
	last   uint64
	closed bool
}

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
	s := &SQLite{path: filepath.Join(dir.Path(), DatabaseFile), fault: opts.Fault, release: release}
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
	if s.w, err = sql.Open("sqlite", s.dsn(false)); err != nil {
		return fmt.Errorf("store: %w", err)
	}
	s.w.SetMaxOpenConns(1)
	if _, err := s.w.Exec(schema); err != nil {
		return fmt.Errorf("store: create schema in %s: %w", s.path, err)
	}
	if _, err := s.w.Exec(`INSERT OR IGNORE INTO meta(key, value) VALUES
		('schema_version', ?), ('last_sequence', 0), ('checkpoint_sequence', 0), ('pruned_max', 0)`, schemaVersion); err != nil {
		return fmt.Errorf("store: %w", err)
	}
	if v, err := s.meta(s.w, "schema_version"); err != nil || v != schemaVersion {
		return fmt.Errorf("store: %s has schema version %d, this build reads %d (%v)", s.path, v, schemaVersion, err)
	}
	if s.r, err = sql.Open("sqlite", s.dsn(true)); err != nil {
		return fmt.Errorf("store: %w", err)
	}
	s.r.SetMaxOpenConns(4)
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
	if s.closed {
		return nil
	}
	s.closed = true
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

func (s *SQLite) at(p Point) error {
	if s.fault == nil {
		return nil
	}
	return s.fault(p)
}

// Commit implements Store.
func (s *SQLite) Commit(r Result) (Committed, error) {
	if len(r.Events) == 0 && len(r.Effects) == 0 {
		return Committed{}, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
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
	if err := s.at(PointBegin); err != nil {
		return Committed{}, err
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
	if err := s.at(PointEvents); err != nil {
		return Committed{}, err
	}
	for i, b := range events {
		if err := project(tx, first+uint64(i), r.Events[i], b); err != nil {
			return Committed{}, err
		}
	}
	if err := s.at(PointTables); err != nil {
		return Committed{}, err
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
	if err := s.at(PointOutbox); err != nil {
		return Committed{}, err
	}
	if err := s.at(PointBeforeCommit); err != nil {
		return Committed{}, err
	}
	if err := tx.Commit(); err != nil {
		return Committed{}, fmt.Errorf("store: commit: %w", err)
	}
	s.last = last
	if err := s.at(PointAfterCommit); err != nil {
		return Committed{}, err
	}
	return Committed{First: first, Last: last}, nil
}

// project writes one event's row in the table of its entity.
func project(tx *sql.Tx, seq uint64, e core.Event, body []byte) error {
	var res sql.Result
	var err error
	switch b := e.Body.(type) {
	case core.Run:
		res, err = tx.Exec(`INSERT INTO runs(id, status, updated_sequence, body) VALUES (?, ?, ?, ?)
			ON CONFLICT(id) DO UPDATE SET status = excluded.status, updated_sequence = excluded.updated_sequence, body = excluded.body`,
			string(b.ID), string(b.Status), seq, body)
	case core.Agent:
		res, err = tx.Exec(`INSERT INTO agents(id, run_id, parent_id, status, updated_sequence, body) VALUES (?, ?, ?, ?, ?, ?)
			ON CONFLICT(id) DO UPDATE SET run_id = excluded.run_id, parent_id = excluded.parent_id, status = excluded.status,
			updated_sequence = excluded.updated_sequence, body = excluded.body`,
			string(b.ID), string(b.Run), string(b.Parent), string(b.Status), seq, body)
	case core.Turn:
		var ended any
		if !b.EndedAt.IsZero() {
			ended = b.EndedAt.UnixNano()
		}
		res, err = tx.Exec(`INSERT INTO turns(agent_id, ordinal, id, run_id, started_at, ended_at, updated_sequence, body)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(agent_id, ordinal) DO UPDATE SET run_id = excluded.run_id, started_at = excluded.started_at,
			ended_at = excluded.ended_at, updated_sequence = excluded.updated_sequence, body = excluded.body
			WHERE turns.id = excluded.id`,
			string(b.Agent), b.Ordinal, string(b.ID), string(e.Run), b.StartedAt.UnixNano(), ended, seq, body)
	case core.Item:
		res, err = tx.Exec(`INSERT INTO items(agent_id, ordinal, id, turn_id, updated_sequence, body) VALUES (?, ?, ?, ?, ?, ?)
			ON CONFLICT(agent_id, ordinal) DO UPDATE SET turn_id = excluded.turn_id,
			updated_sequence = excluded.updated_sequence, body = excluded.body
			WHERE items.id = excluded.id`,
			string(b.Agent), b.Ordinal, string(b.ID), string(b.Turn), seq, body)
	case core.Message:
		res, err = tx.Exec(`INSERT INTO messages(id, run_id, to_agent_id, delivery, seq, updated_sequence, body)
			VALUES (?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(id) DO UPDATE SET run_id = excluded.run_id, to_agent_id = excluded.to_agent_id,
			delivery = excluded.delivery, updated_sequence = excluded.updated_sequence, body = excluded.body`,
			string(b.ID), string(b.Run), string(b.To), string(b.Delivery), seq, seq, body)
	case core.Receipt:
		res, err = tx.Exec(`INSERT INTO command_receipts(command_id, sequence, result) VALUES (?, ?, ?)
			ON CONFLICT(command_id) DO UPDATE SET sequence = excluded.sequence, result = excluded.result`,
			string(b.Command), seq, body)
	default:
		return fmt.Errorf("store: unknown event body %T", e.Body)
	}
	if err != nil {
		return fmt.Errorf("store: project event %d (%T): %w", seq, e.Body, err)
	}
	if n, _ := res.RowsAffected(); n != 1 {
		// Only an item or a turn can write nothing: its ordinal belongs to another id.
		return fmt.Errorf("store: event %d (%T) reuses an ordinal of another entity", seq, e.Body)
	}
	return nil
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

// Recover implements Store.
func (s *SQLite) Recover() (core.State, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return core.State{}, errClosed
	}
	return s.recover()
}

func (s *SQLite) recover() (core.State, error) {
	tx, err := s.r.Begin()
	if err != nil {
		return core.State{}, fmt.Errorf("store: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // A read-only transaction has nothing to undo.
	var state core.State
	ckpt, err := s.meta(tx, "checkpoint_sequence")
	if err != nil {
		return core.State{}, err
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
	if err := read(`SELECT body FROM checkpoint ORDER BY idx`); err != nil {
		return core.State{}, err
	}
	if err := read(`SELECT body FROM events WHERE sequence > ? ORDER BY sequence`, ckpt); err != nil {
		return core.State{}, err
	}
	return state, nil
}

// Claim implements Store.
func (s *SQLite) Claim(now time.Time, limit int) ([]Claimed, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return nil, errClosed
	}
	tx, err := s.w.Begin()
	if err != nil {
		return nil, fmt.Errorf("store: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // After Commit it has nothing to undo.
	rows, err := tx.Query(queryClaim, now.UnixNano(), limit)
	if err != nil {
		return nil, fmt.Errorf("store: %w", err)
	}
	var out []Claimed
	for rows.Next() {
		var id int64
		var b []byte
		if err := rows.Scan(&id, &b); err != nil {
			rows.Close()
			return nil, fmt.Errorf("store: %w", err)
		}
		e, err := wire.DecodeEffect(b)
		if err != nil {
			rows.Close()
			return nil, corruptf("outbox row %d: %v", id, err)
		}
		out = append(out, Claimed{ID: OutboxID(id), Effect: e})
	}
	if err := rows.Close(); err != nil {
		return nil, fmt.Errorf("store: %w", err)
	}
	for _, c := range out {
		if _, err := tx.Exec(`UPDATE outbox SET state = 1 WHERE id = ?`, c.ID); err != nil {
			return nil, fmt.Errorf("store: %w", err)
		}
	}
	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("store: %w", err)
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
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return errClosed
	}
	tx, err := s.w.Begin()
	if err != nil {
		return fmt.Errorf("store: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // After Commit it has nothing to undo.
	for _, id := range ids {
		if _, err := tx.Exec(statement, id); err != nil {
			return fmt.Errorf("store: %w", err)
		}
	}
	return tx.Commit()
}

// NextDue implements Store.
func (s *SQLite) NextDue() (time.Time, bool, error) {
	var nanos sql.NullInt64
	if err := s.r.QueryRow(queryNextDue).Scan(&nanos); err != nil {
		return time.Time{}, false, fmt.Errorf("store: %w", err)
	}
	return time.Unix(0, nanos.Int64).UTC(), nanos.Valid, nil
}

// Compact implements Store.
func (s *SQLite) Compact(p Retention, now time.Time) (Compaction, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return Compaction{}, errClosed
	}
	var out Compaction
	ckpt, err := s.meta(s.w, "checkpoint_sequence")
	if err != nil {
		return Compaction{}, err
	}
	if s.last > ckpt && s.last-ckpt >= p.CheckpointAfter {
		state, err := s.recover()
		if err != nil {
			return Compaction{}, err
		}
		if err := s.writeCheckpoint(state, s.last); err != nil {
			return Compaction{}, err
		}
		ckpt, out.Checkpointed = s.last, true
	}
	out.Checkpoint = ckpt
	dropped, err := s.prune(p, now, ckpt)
	if err != nil {
		return Compaction{}, err
	}
	out.EventsDropped = dropped
	return out, nil
}

// writeCheckpoint replaces the checkpoint with the state at a sequence, in one transaction.
func (s *SQLite) writeCheckpoint(state core.State, seq uint64) error {
	tx, err := s.w.Begin()
	if err != nil {
		return fmt.Errorf("store: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // After Commit it has nothing to undo.
	if _, err := tx.Exec(`DELETE FROM checkpoint`); err != nil {
		return fmt.Errorf("store: %w", err)
	}
	for i, e := range state.Checkpoint() {
		b, err := wire.EncodeEvent(seq, e)
		if err != nil {
			return fmt.Errorf("store: encode checkpoint event %d: %w", i, err)
		}
		if _, _, err := wire.DecodeEvent(b); err != nil {
			return fmt.Errorf("store: checkpoint event %d does not read back: %w", i, err)
		}
		if _, err := tx.Exec(`INSERT INTO checkpoint(idx, body) VALUES (?, ?)`, i+1, b); err != nil {
			return fmt.Errorf("store: %w", err)
		}
	}
	if _, err := tx.Exec(`UPDATE meta SET value = ? WHERE key = 'checkpoint_sequence'`, seq); err != nil {
		return fmt.Errorf("store: %w", err)
	}
	return tx.Commit()
}

// prune drops the events a checkpoint covers whose run is over and old enough, and records how far
// it went for each run, so a reader asking for them is told they are gone.
func (s *SQLite) prune(p Retention, now time.Time, ckpt uint64) (int, error) {
	rows, err := s.w.Query(`SELECT body FROM runs WHERE status IN (?, ?)`, string(core.RunEnded), string(core.RunFailed))
	if err != nil {
		return 0, fmt.Errorf("store: %w", err)
	}
	var eligible []core.RunID
	for rows.Next() {
		var body []byte
		if err := rows.Scan(&body); err != nil {
			rows.Close()
			return 0, fmt.Errorf("store: %w", err)
		}
		_, e, err := decodeBody(body)
		if err != nil {
			rows.Close()
			return 0, err
		}
		run, err := entity[core.Run](e)
		if err != nil {
			rows.Close()
			return 0, err
		}
		if !run.EndedAt.Add(p.KeepEvents).After(now) {
			eligible = append(eligible, run.ID)
		}
	}
	if err := rows.Close(); err != nil {
		return 0, fmt.Errorf("store: %w", err)
	}
	eligible = append(eligible, "")
	slices.Sort(eligible)
	tx, err := s.w.Begin()
	if err != nil {
		return 0, fmt.Errorf("store: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // After Commit it has nothing to undo.
	total := 0
	for _, run := range eligible {
		query, args := `SELECT MAX(sequence) FROM events WHERE run_id = ? AND sequence <= ?`, []any{string(run), ckpt}
		del, delArgs := `DELETE FROM events WHERE run_id = ? AND sequence <= ?`, []any{string(run), ckpt}
		if run == "" {
			// A command refused before any run has no run to end, so its age is its own.
			cutoff := now.Add(-p.KeepEvents).UnixNano()
			query, args = `SELECT MAX(sequence) FROM events WHERE run_id = '' AND sequence <= ? AND at <= ?`, []any{ckpt, cutoff}
			del, delArgs = `DELETE FROM events WHERE run_id = '' AND sequence <= ? AND at <= ?`, []any{ckpt, cutoff}
		}
		var through sql.NullInt64
		if err := tx.QueryRow(query, args...).Scan(&through); err != nil {
			return 0, fmt.Errorf("store: %w", err)
		}
		if !through.Valid {
			continue
		}
		res, err := tx.Exec(del, delArgs...)
		if err != nil {
			return 0, fmt.Errorf("store: %w", err)
		}
		n, _ := res.RowsAffected()
		total += int(n)
		// Dropping events with "at" above a cutoff leaves later ones in place, so "through" for the
		// empty run is the last dropped, and a reader below it is told.
		if _, err := tx.Exec(`INSERT INTO pruned(run_id, through) VALUES (?, ?)
			ON CONFLICT(run_id) DO UPDATE SET through = MAX(through, excluded.through)`, string(run), through.Int64); err != nil {
			return 0, fmt.Errorf("store: %w", err)
		}
		if _, err := tx.Exec(`UPDATE meta SET value = MAX(value, ?) WHERE key = 'pruned_max'`, through.Int64); err != nil {
			return 0, fmt.Errorf("store: %w", err)
		}
	}
	return total, tx.Commit()
}

// Verify implements Store.
func (s *SQLite) Verify() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return errClosed
	}
	state, err := s.recover()
	if err != nil {
		return err
	}
	tx, err := s.r.Begin()
	if err != nil {
		return fmt.Errorf("store: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // A read-only transaction has nothing to undo.
	if err := s.verifyLog(tx); err != nil {
		return err
	}
	if err := verifySummary(tx, state); err != nil {
		return err
	}
	return s.verifyProjection(tx)
}

// verifyLog checks the events: ascending, with no gap above the checkpoint, each equal to its
// columns, and the end where the store says.
func (s *SQLite) verifyLog(tx *sql.Tx) error {
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
	if end := max(next-1, ckpt); end != s.last {
		return corruptf("the log ends at %d and the store says %d", end, s.last)
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

// verifyProjection checks that each entity the retained events write holds, in its table, the
// bytes of the last such event: the tables are what a fold of the log leaves.
func (s *SQLite) verifyProjection(tx *sql.Tx) error {
	want := newTables()
	rows, err := tx.Query(`SELECT sequence, body FROM events ORDER BY sequence`)
	if err != nil {
		return fmt.Errorf("store: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var seq uint64
		var body []byte
		if err := rows.Scan(&seq, &body); err != nil {
			return fmt.Errorf("store: %w", err)
		}
		_, e, err := decodeBody(body)
		if err != nil {
			return err
		}
		if err := want.apply(seq, e, body); err != nil {
			return err
		}
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("store: %w", err)
	}
	same := func(what, key string, w row, query string, args ...any) error {
		var body []byte
		var last uint64
		err := tx.QueryRow(query, args...).Scan(&body, &last)
		switch {
		case errors.Is(err, sql.ErrNoRows):
			return corruptf("%s %s is written by the log and missing from its table", what, key)
		case err != nil:
			return fmt.Errorf("store: %w", err)
		case last != w.last || !slices.Equal(body, w.body):
			return corruptf("%s %s differs from the last event that wrote it (sequence %d)", what, key, w.last)
		}
		return nil
	}
	for id, w := range want.runs {
		if err := same("run", string(id), w, `SELECT body, updated_sequence FROM runs WHERE id = ?`, string(id)); err != nil {
			return err
		}
	}
	for id, w := range want.agents {
		if err := same("agent", string(id), w, `SELECT body, updated_sequence FROM agents WHERE id = ?`, string(id)); err != nil {
			return err
		}
	}
	for id, w := range want.turns {
		t := w.event.Body.(core.Turn)
		if err := same("turn", string(id), w, `SELECT body, updated_sequence FROM turns WHERE agent_id = ? AND ordinal = ?`, string(t.Agent), t.Ordinal); err != nil {
			return err
		}
	}
	for id, w := range want.items {
		i := w.event.Body.(core.Item)
		if err := same("item", string(id), w, `SELECT body, updated_sequence FROM items WHERE agent_id = ? AND ordinal = ?`, string(i.Agent), i.Ordinal); err != nil {
			return err
		}
	}
	for id, w := range want.receipts {
		if err := same("receipt", string(id), w, `SELECT result, sequence FROM command_receipts WHERE command_id = ?`, string(id)); err != nil {
			return err
		}
	}
	for id, w := range want.messages {
		var body []byte
		var last, first uint64
		err := tx.QueryRow(`SELECT body, updated_sequence, seq FROM messages WHERE id = ?`, string(id)).Scan(&body, &last, &first)
		switch {
		case errors.Is(err, sql.ErrNoRows):
			return corruptf("message %s is written by the log and missing from its table", id)
		case err != nil:
			return fmt.Errorf("store: %w", err)
		case last != w.last || !slices.Equal(body, w.body):
			return corruptf("message %s differs from the last event that wrote it (sequence %d)", id, w.last)
		case first > w.first:
			// A message's place is its first event, which may have been dropped with its run. The
			// table keeps the older place, so it can only be at or before what the log shows.
			return corruptf("message %s is placed at %d, after its first retained event at %d", id, first, w.first)
		}
	}
	return nil
}
