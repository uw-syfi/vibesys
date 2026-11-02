package store

import (
	"database/sql"
	"fmt"
	"math"
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
func scanEvents(rows *sql.Rows, each func(seq uint64, e core.Event, rest []any) error, rest ...any) error {
	defer rows.Close()
	for rows.Next() {
		var body []byte
		if err := rows.Scan(append([]any{&body}, rest...)...); err != nil {
			return fmt.Errorf("store: %w", err)
		}
		seq, e, err := decodeBody(body)
		if err != nil {
			return err
		}
		if err := each(seq, e, rest); err != nil {
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
		rows, err := tx.Query(screenQueries["snapshot runs"] + ` ORDER BY id`)
		if err != nil {
			return fmt.Errorf("store: %w", err)
		}
		if err := scanEvents(rows, func(_ uint64, e core.Event, _ []any) error {
			out.Runs = append(out.Runs, e.Body.(core.Run))
			return nil
		}); err != nil {
			return err
		}
		rows, err = tx.Query(screenQueries["snapshot agents"] + ` ORDER BY id`)
		if err != nil {
			return fmt.Errorf("store: %w", err)
		}
		return scanEvents(rows, func(_ uint64, e core.Event, _ []any) error {
			out.Agents = append(out.Agents, e.Body.(core.Agent))
			return nil
		})
	})
	return out, err
}

// EventsAfter implements Reader.
func (s *SQLite) EventsAfter(run core.RunID, after uint64, limit int) ([]EventRecord, error) {
	var out []EventRecord
	err := s.reading(func(tx *sql.Tx) error {
		var through uint64
		if run == "" {
			var err error
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
		var err error
		if run == "" {
			rows, err = tx.Query(queryAllEvents, after, limit)
		} else {
			rows, err = tx.Query(queryRunEvents, string(run), after, limit)
		}
		if err != nil {
			return fmt.Errorf("store: %w", err)
		}
		return scanEvents(rows, func(seq uint64, e core.Event, _ []any) error {
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
			page.Items = append(page.Items, e.Body.(core.Item))
		}
		return rows.Err()
	})
	for i, j := 0, len(page.Items)-1; i < j; i, j = i+1, j-1 {
		page.Items[i], page.Items[j] = page.Items[j], page.Items[i]
	}
	return page, err
}

// Messages implements Reader.
func (s *SQLite) Messages(run core.RunID, after uint64, limit int) ([]MessageRecord, error) {
	var out []MessageRecord
	err := s.reading(func(tx *sql.Tx) error {
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
			out = append(out, MessageRecord{Seq: seq, Message: e.Body.(core.Message)})
		}
		return rows.Err()
	})
	return out, err
}

// Turns implements Reader.
func (s *SQLite) Turns(run core.RunID, from, to time.Time, limit int) (TurnWindow, error) {
	var out TurnWindow
	limit = pageLimit(limit, MaxWindowTurns)
	err := s.reading(func(tx *sql.Tx) error {
		rows, err := tx.Query(queryTurns, string(run), from.UnixNano(), to.UnixNano(), limit+1)
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
			out.Turns = append(out.Turns, e.Body.(core.Turn))
		}
		return rows.Err()
	})
	return out, err
}
