package store

import (
	"database/sql"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// projection says how one kind of event is written into its table, and is the one place that says
// which columns are copies of the body. project writes from it and Verify checks against it, so
// the two cannot disagree about what a row holds.
type projection struct {
	table string
	// key are the columns that name a row, and dup the columns copied out of the body for the
	// indexes. values returns both, in that order, from an event. first names a column written on
	// insert and never changed after, which a message uses for its place in the order.
	key, dup []string
	first    string
	values   func(e core.Event) (key, dup []any)
	// guard is a condition on the existing row that must hold for an update to apply. An item or turn
	// whose key is held by another id applies nothing, which is how a clash is refused.
	guard string
	// upsert is the statement, built once from the fields above.
	upsert string
}

// nanos is a time as a column: unix nanoseconds, and NULL for the zero time.
func nanos(t time.Time) any {
	if t.IsZero() {
		return nil
	}
	return t.UnixNano()
}

// projections are the tables that hold one entity per row, keyed by what the body names. The
// command_receipts table is not here: its columns differ and it has no copy of the body to check.
var projections = func() map[string]*projection {
	all := []*projection{
		{
			table: "runs", key: []string{"id"}, dup: []string{"status"},
			values: func(e core.Event) ([]any, []any) {
				b := e.Body.(core.Run)
				return []any{string(b.ID)}, []any{string(b.Status)}
			},
		},
		{
			table: "agents", key: []string{"id"}, dup: []string{"run_id", "parent_id", "status"},
			values: func(e core.Event) ([]any, []any) {
				b := e.Body.(core.Agent)
				return []any{string(b.ID)}, []any{string(b.Run), string(b.Parent), string(b.Status)}
			},
		},
		{
			table: "turns", key: []string{"agent_id", "ordinal"}, dup: []string{"id", "run_id", "started_at", "ended_at"},
			guard: "turns.id = excluded.id",
			values: func(e core.Event) ([]any, []any) {
				b := e.Body.(core.Turn)
				return []any{string(b.Agent), int64(b.Ordinal)},
					[]any{string(b.ID), string(e.Run), b.StartedAt.UnixNano(), nanos(b.EndedAt)}
			},
		},
		{
			table: "items", key: []string{"agent_id", "ordinal"}, dup: []string{"id", "turn_id"},
			guard: "items.id = excluded.id",
			values: func(e core.Event) ([]any, []any) {
				b := e.Body.(core.Item)
				return []any{string(b.Agent), int64(b.Ordinal)}, []any{string(b.ID), string(b.Turn)}
			},
		},
		{
			table: "check_ins", key: []string{"id"}, dup: []string{"run_id", "viewer_id"},
			values: func(e core.Event) ([]any, []any) {
				b := e.Body.(core.CheckIn)
				return []any{string(b.ID)}, []any{string(b.Run), string(b.Viewer.ID)}
			},
		},
		{
			table: "messages", key: []string{"id"}, dup: []string{"run_id", "to_agent_id", "delivery"}, first: "seq",
			values: func(e core.Event) ([]any, []any) {
				b := e.Body.(core.Message)
				return []any{string(b.ID)}, []any{string(b.Run), string(b.To), string(b.Delivery)}
			},
		},
	}
	out := map[string]*projection{}
	for _, p := range all {
		cols := slices.Concat(p.key, p.dup)
		if p.first != "" {
			cols = append(cols, p.first)
		}
		cols = append(cols, "updated_sequence", "body")
		set := []string{}
		for _, c := range append(slices.Clone(p.dup), "updated_sequence", "body") {
			set = append(set, c+" = excluded."+c)
		}
		stmt := fmt.Sprintf("INSERT INTO %s(%s) VALUES (%s) ON CONFLICT(%s) DO UPDATE SET %s",
			p.table, strings.Join(cols, ", "), strings.TrimSuffix(strings.Repeat("?, ", len(cols)), ", "),
			strings.Join(p.key, ", "), strings.Join(set, ", "))
		if p.guard != "" {
			stmt += " WHERE " + p.guard
		}
		p.upsert = stmt
		out[p.table] = p
	}
	return out
}()

// projectionOf finds the table an event body is written to. Receipts have none: see project.
func projectionOf(e core.Event) (*projection, bool) {
	var table string
	switch e.Body.(type) {
	case core.Run:
		table = "runs"
	case core.Agent:
		table = "agents"
	case core.Turn:
		table = "turns"
	case core.Item:
		table = "items"
	case core.Message:
		table = "messages"
	case core.CheckIn:
		table = "check_ins"
	default:
		return nil, false
	}
	return projections[table], true
}

// project writes one event's row in the table of its entity.
func project(tx *sql.Tx, seq uint64, e core.Event, body []byte) error {
	var res sql.Result
	var err error
	if b, ok := e.Body.(core.Receipt); ok {
		res, err = tx.Exec(`INSERT INTO command_receipts(command_id, sequence, result) VALUES (?, ?, ?)
			ON CONFLICT(command_id) DO UPDATE SET sequence = excluded.sequence, result = excluded.result`,
			string(b.Command), seq, body)
	} else if p, ok := projectionOf(e); ok {
		key, dup := p.values(e)
		args := slices.Concat(key, dup)
		if p.first != "" {
			args = append(args, seq)
		}
		res, err = tx.Exec(p.upsert, append(args, seq, body)...)
	} else {
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

// verifyProjection checks every table against the retained log, and the index columns of every row
// against the row's own body. The tables are what a fold of the log leaves, with this exception:
// Compact drops the events of a finished run and keeps its rows (see Retention), so a row with no
// retained event is accepted only when its run was pruned at or after its last write.
func verifyProjection(tx *sql.Tx) error {
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
	pruned, err := loadPruned(tx)
	if err != nil {
		return err
	}
	if err := verifyReceipts(tx, want); err != nil {
		return err
	}
	for _, name := range []string{"runs", "agents", "turns", "items", "messages", "check_ins"} {
		if err := verifyTable(tx, projections[name], want, pruned); err != nil {
			return err
		}
	}
	return nil
}

func loadPruned(tx *sql.Tx) (map[core.RunID]uint64, error) {
	rows, err := tx.Query(`SELECT run_id, through FROM pruned`)
	if err != nil {
		return nil, fmt.Errorf("store: %w", err)
	}
	defer rows.Close()
	out := map[core.RunID]uint64{}
	for rows.Next() {
		var run string
		var through uint64
		if err := rows.Scan(&run, &through); err != nil {
			return nil, fmt.Errorf("store: %w", err)
		}
		out[core.RunID(run)] = through
	}
	return out, rows.Err()
}

// lookup finds the retained projection's row for the entity an event writes.
func (t *tables) lookup(e core.Event) (row, bool) {
	var r row
	var ok bool
	switch b := e.Body.(type) {
	case core.Run:
		r, ok = t.runs[b.ID]
	case core.Agent:
		r, ok = t.agents[b.ID]
	case core.Turn:
		r, ok = t.turns[b.ID]
	case core.Item:
		r, ok = t.items[b.ID]
	case core.Message:
		r, ok = t.messages[b.ID]
	case core.CheckIn:
		r, ok = t.checkIns[b.ID]
	}
	return r, ok
}

func (t *tables) count(table string) int {
	switch table {
	case "runs":
		return len(t.runs)
	case "agents":
		return len(t.agents)
	case "turns":
		return len(t.turns)
	case "items":
		return len(t.items)
	case "check_ins":
		return len(t.checkIns)
	default:
		return len(t.messages)
	}
}

// verifyTable reads every row of one table. Each row must: decode to the kind of event the table
// holds; carry, in its key and index columns, the values the body gives; say the sequence the
// body says; and either equal the last retained event that wrote the entity, or have none because
// its run's events were pruned. Every entity the retained log writes must have its row.
func verifyTable(tx *sql.Tx, p *projection, want *tables, pruned map[core.RunID]uint64) error {
	cols := slices.Concat(p.key, p.dup)
	if p.first != "" {
		cols = append(cols, p.first)
	}
	cols = append(cols, "updated_sequence", "body")
	rows, err := tx.Query(fmt.Sprintf("SELECT %s FROM %s", strings.Join(cols, ", "), p.table))
	if err != nil {
		return fmt.Errorf("store: %w", err)
	}
	defer rows.Close()
	retainedRows := 0
	for rows.Next() {
		got := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range got {
			ptrs[i] = &got[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			return fmt.Errorf("store: %w", err)
		}
		body, _ := got[len(cols)-1].([]byte)
		last, _ := got[len(cols)-2].(int64)
		seq, e, err := decodeBody(body)
		if err != nil {
			return err
		}
		if pe, ok := projectionOf(e); !ok || pe != p {
			return corruptf("%s holds a row whose body is a %T", p.table, e.Body)
		}
		key, dup := p.values(e)
		expect := slices.Concat(key, dup)
		for i, c := range slices.Concat(p.key, p.dup) {
			if !sameColumn(got[i], expect[i]) {
				return corruptf("%s row %v: column %s holds %v, its body says %v", p.table, key, c, got[i], expect[i])
			}
		}
		if uint64(last) != seq {
			return corruptf("%s row %v: updated_sequence is %d and its body is event %d", p.table, key, last, seq)
		}
		w, retained := want.lookup(e)
		if !retained {
			through, ok := pruned[e.Run]
			if !ok || seq > through {
				return corruptf("%s row %v (event %d) is written by no event of the log, and its run was not pruned that far", p.table, key, seq)
			}
			continue
		}
		retainedRows++
		if w.last != seq || !slices.Equal(w.body, body) {
			return corruptf("%s row %v differs from the last event that wrote it (sequence %d)", p.table, key, w.last)
		}
		if p.first != "" {
			// A message's place is its first event, which may have been dropped with its run. The
			// table keeps the older place, so it can only be at or before what the log shows.
			if first, _ := got[len(cols)-3].(int64); uint64(first) > w.first {
				return corruptf("message %v is placed at %d, after its first retained event at %d", key, first, w.first)
			}
		}
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("store: %w", err)
	}
	// Rows were matched to entities one to one (a key is unique), so a shortfall says the log writes
	// an entity that has no row.
	if n := want.count(p.table); retainedRows < n {
		return corruptf("%s: the log writes %d entities and %d have a row", p.table, n, retainedRows)
	}
	return nil
}

// sameColumn compares what a driver scanned with what the body gives.
func sameColumn(got, want any) bool {
	switch w := want.(type) {
	case nil:
		return got == nil
	case int64:
		g, ok := got.(int64)
		return ok && g == w
	case string:
		switch g := got.(type) {
		case string:
			return g == w
		case []byte:
			return string(g) == w
		}
	}
	return false
}

// verifyReceipts checks the receipts the retained log writes against their rows. A receipt row with
// no retained event is held to the state by Verify's summary check, which compares both ways.
func verifyReceipts(tx *sql.Tx, want *tables) error {
	for id, w := range want.receipts {
		var body []byte
		var last uint64
		err := tx.QueryRow(`SELECT result, sequence FROM command_receipts WHERE command_id = ?`, string(id)).Scan(&body, &last)
		switch {
		case errors.Is(err, sql.ErrNoRows):
			return corruptf("receipt %s is written by the log and missing from its table", id)
		case err != nil:
			return fmt.Errorf("store: %w", err)
		case last != w.last || !slices.Equal(body, w.body):
			return corruptf("receipt %s differs from the last event that wrote it (sequence %d)", id, w.last)
		}
	}
	return nil
}
