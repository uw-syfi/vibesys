package store

import (
	"cmp"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"math"
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// reading runs f in one read transaction, so every query in it sees the same commit.
func (s *SQLite) reading(f func(tx *sql.Tx) error) error {
	tx, err := s.r.Begin()
	if err != nil {
		return fmt.Errorf("store: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // A read-only transaction has nothing to undo.
	return f(tx)
}

// scanEvents decodes the first column of each row as an event.
func scanEvents(rows *sql.Rows, each func(seq uint64, e core.Event) error) error {
	defer rows.Close()
	for rows.Next() {
		var body []byte
		if err := rows.Scan(&body); err != nil {
			return fmt.Errorf("store: %w", err)
		}
		seq, e, err := decodeBody(body)
		if err != nil {
			return err
		}
		if err := each(seq, e); err != nil {
			return err
		}
	}
	return rows.Err()
}

// Snapshot implements Reader.
func (s *SQLite) Snapshot() (Snapshot, error) {
	var out Snapshot
	err := s.reading(func(tx *sql.Tx) error {
		var err error
		if out.Sequence, err = s.meta(tx, "last_sequence"); err != nil {
			return err
		}
		type listed struct {
			run  core.Run
			last uint64
		}
		var finished []listed
		for _, status := range []core.RunStatus{core.RunStarting, core.RunRunning, core.RunStopping, core.RunEnded, core.RunFailed} {
			limit := MaxFinishedRuns
			if status.Live() || status == core.RunStopping {
				limit = math.MaxInt32
			}
			rows, err := tx.Query(querySnapshotRuns, string(status), limit)
			if err != nil {
				return fmt.Errorf("store: %w", err)
			}
			err = scanEvents(rows, func(last uint64, e core.Event) error {
				run, err := entity[core.Run](e)
				if err != nil {
					return err
				}
				if status.Final() {
					finished = append(finished, listed{run, last})
				} else {
					out.Runs = append(out.Runs, run)
				}
				return nil
			})
			if err != nil {
				return err
			}
		}
		slices.SortFunc(finished, func(a, b listed) int { return cmp.Compare(b.last, a.last) })
		for _, f := range finished[:min(len(finished), MaxFinishedRuns)] {
			out.Runs = append(out.Runs, f.run)
		}
		slices.SortFunc(out.Runs, func(a, b core.Run) int { return cmp.Compare(a.ID, b.ID) })
		for _, run := range out.Runs {
			rows, err := tx.Query(querySnapshotAgents, string(run.ID))
			if err != nil {
				return fmt.Errorf("store: %w", err)
			}
			if err := scanEvents(rows, func(_ uint64, e core.Event) error {
				a, err := entity[core.Agent](e)
				out.Agents = append(out.Agents, a)
				return err
			}); err != nil {
				return err
			}
		}
		slices.SortFunc(out.Agents, func(a, b core.Agent) int { return cmp.Compare(a.ID, b.ID) })
		return nil
	})
	return out, err
}

// Settings are the connection settings that make the file safe and fast to share between the writer
// and the readers.
type Settings struct {
	JournalMode   string
	Synchronous   int
	BusyTimeoutMS int
	QueryOnly     int
}

func readSettings(q func(pragma string, dest any) error) (Settings, error) {
	var out Settings
	for pragma, dest := range map[string]any{
		"journal_mode": &out.JournalMode, "synchronous": &out.Synchronous,
		"busy_timeout": &out.BusyTimeoutMS, "query_only": &out.QueryOnly,
	} {
		if err := q("PRAGMA "+pragma, dest); err != nil {
			return out, fmt.Errorf("store: %s: %w", pragma, err)
		}
	}
	return out, nil
}

// Settings reports the settings of the writer's connection and of every connection the readers'
// pool can hold, so that a test pins them all: a reader that quietly opened with defaults would
// block the writer or read a stale file.
func (s *SQLite) Settings() (writer Settings, readers []Settings, err error) {
	ctx := context.Background()
	wc, err := s.w.Conn(ctx)
	if err != nil {
		return writer, nil, fmt.Errorf("store: %w", err)
	}
	defer wc.Close()
	if writer, err = readSettings(func(p string, d any) error { return wc.QueryRowContext(ctx, p).Scan(d) }); err != nil {
		return writer, nil, err
	}
	// Hold every connection the pool allows at once, so that each is a different one.
	var held []*sql.Conn
	defer func() {
		for _, c := range held {
			c.Close()
		}
	}()
	for range readerConns {
		c, err := s.r.Conn(ctx)
		if err != nil {
			return writer, nil, fmt.Errorf("store: %w", err)
		}
		held = append(held, c)
		got, err := readSettings(func(p string, d any) error { return c.QueryRowContext(ctx, p).Scan(d) })
		if err != nil {
			return writer, nil, err
		}
		readers = append(readers, got)
	}
	return writer, readers, nil
}

// Explain returns SQLite's query plan for each read that serves a screen, so that a test can hold
// them to searches on an index.
func (s *SQLite) Explain() (map[string][]string, error) {
	args := map[string]int{
		"snapshot runs": 2, "snapshot agents": 1, "events of a run": 3, "events of all": 2, "items page": 3,
		"messages of a run": 3, "message by id": 1, "turns in window": 4, "receipt": 1, "claim": 2, "checkpoint": 1, "next due": 0,
	}
	out := map[string][]string{}
	for name, query := range screenQueries {
		params := make([]any, args[name])
		for i := range params {
			params[i] = 1
		}
		rows, err := s.r.Query(`EXPLAIN QUERY PLAN `+query, params...)
		if err != nil {
			return nil, fmt.Errorf("store: explain %s: %w", name, err)
		}
		for rows.Next() {
			var id, parent, unused int
			var detail string
			if err := rows.Scan(&id, &parent, &unused, &detail); err != nil {
				rows.Close()
				return nil, fmt.Errorf("store: %w", err)
			}
			out[name] = append(out[name], detail)
		}
		if err := rows.Close(); err != nil {
			return nil, fmt.Errorf("store: %w", err)
		}
	}
	return out, nil
}

// EventsAfter implements Reader.
func (s *SQLite) EventsAfter(run core.RunID, after uint64, limit int) ([]EventRecord, error) {
	var out []EventRecord
	err := s.reading(func(tx *sql.Tx) error {
		last, err := s.meta(tx, "last_sequence")
		if err != nil {
			return err
		}
		if after > last {
			return ErrAheadOfLog
		}
		var through uint64
		if run == "" {
			if through, err = s.meta(tx, "pruned_max"); err != nil {
				return err
			}
		} else {
			var v sql.NullInt64
			if err := tx.QueryRow(`SELECT through FROM pruned WHERE run_id = ?`, string(run)).Scan(&v); err != nil && err != sql.ErrNoRows {
				return fmt.Errorf("store: %w", err)
			}
			through = uint64(v.Int64)
		}
		if after < through {
			return ErrCompacted
		}
		limit = pageLimit(limit, MaxPageEvents)
		var rows *sql.Rows
		if run == "" {
			rows, err = tx.Query(queryAllEvents, after, limit)
		} else {
			rows, err = tx.Query(queryRunEvents, string(run), after, limit)
		}
		if err != nil {
			return fmt.Errorf("store: %w", err)
		}
		return scanEvents(rows, func(seq uint64, e core.Event) error {
			out = append(out, EventRecord{Sequence: seq, Event: e})
			return nil
		})
	})
	return out, err
}

// Items implements Reader.
func (s *SQLite) Items(agent core.AgentID, before uint32, limit int) (ItemPage, error) {
	var page ItemPage
	bound := int64(before)
	if before == 0 {
		bound = math.MaxUint32 + 1
	}
	limit = pageLimit(limit, MaxPageItems)
	err := s.reading(func(tx *sql.Tx) error {
		// One more than the limit, to learn whether an older page exists.
		rows, err := tx.Query(queryItems, string(agent), bound, limit+1)
		if err != nil {
			return fmt.Errorf("store: %w", err)
		}
		size := 0
		defer rows.Close()
		for rows.Next() {
			var body []byte
			if err := rows.Scan(&body); err != nil {
				return fmt.Errorf("store: %w", err)
			}
			if len(page.Items) == limit || (len(page.Items) > 0 && size+len(body) > MaxPageBytes) {
				page.HasOlder = true
				break
			}
			_, e, err := decodeBody(body)
			if err != nil {
				return err
			}
			size += len(body)
			item, err := entity[core.Item](e)
			if err != nil {
				return err
			}
			page.Items = append(page.Items, item)
		}
		return rows.Err()
	})
	for i, j := 0, len(page.Items)-1; i < j; i, j = i+1, j-1 {
		page.Items[i], page.Items[j] = page.Items[j], page.Items[i]
	}
	return page, err
}

// Message implements Reader.
func (s *SQLite) Message(id core.MessageID) (core.Message, bool, error) {
	var msg core.Message
	found := false
	err := s.reading(func(tx *sql.Tx) error {
		var body []byte
		switch err := tx.QueryRow(queryMessage, string(id)).Scan(&body); {
		case errors.Is(err, sql.ErrNoRows):
			return nil
		case err != nil:
			return fmt.Errorf("store: %w", err)
		}
		_, e, err := decodeBody(body)
		if err != nil {
			return err
		}
		msg, err = entity[core.Message](e)
		found = err == nil
		return err
	})
	return msg, found, err
}

// Messages implements Reader.
func (s *SQLite) Messages(run core.RunID, after uint64, limit int) ([]MessageRecord, error) {
	var out []MessageRecord
	err := s.reading(func(tx *sql.Tx) error {
		last, err := s.meta(tx, "last_sequence")
		if err != nil {
			return err
		}
		if after > last {
			return ErrAheadOfLog
		}
		rows, err := tx.Query(queryMessages, string(run), after, pageLimit(limit, MaxPageMessages))
		if err != nil {
			return fmt.Errorf("store: %w", err)
		}
		defer rows.Close()
		for rows.Next() {
			var body []byte
			var seq uint64
			if err := rows.Scan(&body, &seq); err != nil {
				return fmt.Errorf("store: %w", err)
			}
			_, e, err := decodeBody(body)
			if err != nil {
				return err
			}
			msg, err := entity[core.Message](e)
			if err != nil {
				return err
			}
			out = append(out, MessageRecord{Seq: seq, Message: msg})
		}
		return rows.Err()
	})
	return out, err
}

// windowBound is a time as a window bound of the tables. A time outside what int64 nanoseconds can say is clamped to
// the nearest bound, so that the zero time and a far-future time stay the ends of a window and do
// not wrap around: time.Time.UnixNano is undefined for them, and for the year 9999 it came out
// negative, which made the window of a run's whole history empty.
func windowBound(t time.Time) int64 {
	switch {
	case t.Before(time.Unix(0, math.MinInt64)):
		return math.MinInt64
	case t.After(time.Unix(0, math.MaxInt64)):
		return math.MaxInt64
	}
	return t.UnixNano()
}

// Turns implements Reader.
func (s *SQLite) Turns(run core.RunID, from, to time.Time, limit int) (TurnWindow, error) {
	var out TurnWindow
	limit = pageLimit(limit, MaxWindowTurns)
	err := s.reading(func(tx *sql.Tx) error {
		rows, err := tx.Query(queryTurns, string(run), windowBound(from), windowBound(to), limit+1)
		if err != nil {
			return fmt.Errorf("store: %w", err)
		}
		defer rows.Close()
		for rows.Next() {
			var body []byte
			var started int64
			var id string
			if err := rows.Scan(&body, &started, &id); err != nil {
				return fmt.Errorf("store: %w", err)
			}
			if len(out.Turns) == limit {
				out.Truncated = true
				break
			}
			_, e, err := decodeBody(body)
			if err != nil {
				return err
			}
			turn, err := entity[core.Turn](e)
			if err != nil {
				return err
			}
			out.Turns = append(out.Turns, turn)
		}
		return rows.Err()
	})
	return out, err
}
