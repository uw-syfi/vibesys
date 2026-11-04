package daemon

import (
	"fmt"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
)

// upgradeLocations records the current choice once for pre-location runs. The typed Run event
// makes recovery and projections agree; the log identifies the compatibility migration.
func (e *Engine) upgradeLocations() error {
	if e.Location.CLI == "" {
		return nil
	}
	for _, run := range e.state.Runs() {
		if run.Status.Final() || run.Location != (core.CliLocation{}) {
			continue
		}
		run.Location = e.Location
		event := core.Event{Run: run.ID, At: e.Clock.Now(), Body: run}
		committed, err := e.Store.Commit(store.Result{Events: []core.Event{event}})
		if err != nil {
			return fmt.Errorf("record legacy CLI location for run %s: %w", run.ID, err)
		}
		e.state = core.Replay(e.state, []core.Event{event})
		e.last = committed.Last
		e.Log.Info("migrated legacy run CLI location", "run", run.ID, "migration", "identity-location-v1", "cli", run.Location.CLI)
	}
	return nil
}
