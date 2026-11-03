package store

import (
	"database/sql"
	"fmt"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// upgradeIdentity upgrades schema 2's queued launches before the strict decoder sees them.
// The version and every changed row commit together, so a crash repeats the entire upgrade.
func (s *SQLite) upgradeIdentity() error {
	return s.write(func(tx *sql.Tx) error {
		state, _, err := recoverFrom(tx)
		if err != nil {
			return err
		}
		rows, err := tx.Query(`SELECT id, effect FROM outbox`)
		if err != nil {
			return err
		}
		type update struct {
			id   int64
			body []byte
		}
		var updates []update
		var failures []core.Event
		var dropped []int64
		for rows.Next() {
			var id int64
			var body []byte
			if err := rows.Scan(&id, &body); err != nil {
				rows.Close()
				return err
			}
			body, owner, err := wire.UpgradeEffectV2(body, state)
			if owner == "" {
				if err != nil {
					rows.Close()
					return corruptf("identity upgrade outbox row %d: %v", id, err)
				}
				continue
			}
			run, ok := state.Run(owner)
			if !ok {
				rows.Close()
				return corruptf("identity upgrade outbox row %d: missing owning run %s; restore the run before restarting", id, owner)
			}

			if err != nil {
				run.Status, run.StopReason, run.EndedAt = core.RunFailed, core.StopLeadGone, run.CreatedAt
				failures = append(failures, core.Event{Run: run.ID, At: run.CreatedAt, Body: run})
				for _, agent := range state.Agents(run.ID) {
					agent.Status, agent.EndedAt = core.AgentFailed, run.CreatedAt
					agent.EndDetail = fmt.Sprintf("identity upgrade could not migrate queued launch row %d: %v", id, err)
					failures = append(failures, core.Event{Run: run.ID, At: run.CreatedAt, Body: agent})
				}
				dropped = append(dropped, id)
				continue
			}
			updates = append(updates, update{id, body})
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		for _, u := range updates {
			if _, err := tx.Exec(`UPDATE outbox SET effect = ? WHERE id = ?`, u.body, u.id); err != nil {
				return err
			}
		}
		for _, id := range dropped {
			if _, err := tx.Exec(`DELETE FROM outbox WHERE id = ?`, id); err != nil {
				return err
			}
		}
		seq, err := s.meta(tx, "last_sequence")
		if err != nil {
			return err
		}
		for _, event := range failures {
			seq++
			body, err := wire.EncodeEvent(seq, event)
			if err != nil {
				return err
			}
			if _, err := tx.Exec(`INSERT INTO events(sequence, run_id, command_id, at, body) VALUES (?, ?, '', ?, ?)`, seq, string(event.Run), event.At.UnixNano(), body); err != nil {
				return err
			}
			if err := project(tx, seq, event, body); err != nil {
				return err
			}
		}
		if _, err := tx.Exec(`UPDATE meta SET value = ? WHERE key = 'last_sequence'`, seq); err != nil {
			return err
		}
		s.last = seq
		_, err = tx.Exec(`UPDATE meta SET value = 3 WHERE key = 'schema_version'`)
		return err
	})
}
