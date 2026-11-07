package daemon

import (
	"fmt"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/cost"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/window"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// maxFailureBytes bounds what a failed check-in says, as the contract does.
const maxFailureBytes = 500

// computeWindowFacts carries out a ComputeWindowFacts: it reads the records the facts are made of,
// works them out with window.Facts, and answers the core with them, or with the reason they cannot be
// worked out. The answer is what is frozen in the check-in, so an effect carried out twice, or after
// the check-in was settled, changes nothing: the core drops a second answer.
//
// What it reads that the core does not hold: the job log of the run's task as the host last read it (not
// the capped copy that clients are served), the run's finished turns and the run's messages, from the
// store. A job log that cannot be read now fails the check-in rather than leaving its measurements and
// plans out of the facts, because a window that says nothing was measured is a different statement from a
// window the host could not look into. A store that cannot be read is an error, which retries the effect.
func (d *Daemon) computeWindowFacts(e core.ComputeWindowFacts) error {
	state := d.State()
	check, ok := state.CheckIn(e.CheckIn)
	if _, pending := check.Outcome.(core.CheckInPending); !ok || !pending {
		return nil
	}
	run, ok := state.Run(e.Run)
	if !ok {
		return fmt.Errorf("window facts of check-in %s: no run %s", e.CheckIn, e.Run)
	}
	view, why := d.readableView(run.ID)
	if why != "" {
		_, err := d.Apply(core.WindowFactsFailed{CheckIn: e.CheckIn, Detail: why})
		return err
	}
	ledger := &cost.Ledger{}
	for _, a := range state.Agents(run.ID) {
		ledger.Agent(a)
	}
	if err := d.Engine.readTurns(state, run.ID, ledger); err != nil {
		return err
	}
	records, err := d.Engine.readMessages(run.ID)
	if err != nil {
		return err
	}
	messages := make([]core.Message, 0, len(records))
	for _, r := range records {
		messages = append(messages, r.Message)
	}
	facts := window.Facts(window.Input{
		Run: run.ID, Since: e.Since, Until: e.Until, View: view, Ledger: ledger, Agents: state.Agents(run.ID), Messages: messages,
	})
	// A window with more in it than the contract allows is not cut short, which would say less than
	// happened: the check-in fails, naming the limit, and the viewer sees that the host could not keep it.
	candidate := core.CheckIn{ID: check.ID, Run: check.Run, Viewer: check.Viewer, Since: check.Since, At: check.At, Outcome: core.CheckInReady{Facts: facts}, Ask: check.Ask, Report: check.Report}
	if err := wire.Validate(wire.CheckInMessage(candidate)); err != nil {
		_, err := d.Apply(core.WindowFactsFailed{CheckIn: e.CheckIn, Detail: clip("the facts of this window are more than a check-in can keep: " + err.Error())})
		return err
	}
	_, err = d.Apply(core.WindowFactsReady{CheckIn: e.CheckIn, Facts: facts})
	return err
}

// readableView returns the run's whole job log when it can be relied on, and otherwise why not, in words
// for a viewer. A view that is unreadable still holds the facts it last had, but they are as old as the
// last read, so a window is worked out from a view that is current and not from one that is not.
func (d *Daemon) readableView(run core.RunID) (search.View, string) {
	if d.Search == nil {
		return search.View{}, "this host reads no job log for its runs"
	}
	view, revision := d.Search.Get(run)
	if revision == 0 {
		return search.View{}, "the job log of this run's task has not been read yet: try again in a moment"
	}
	if view.Reason != core.Readable {
		return search.View{}, clip("the job log of this run's task cannot be read now (" + reasonWords(view.Reason) + "): try again later")
	}
	return view, ""
}

// reasonWords says why a view cannot be read, in a few words.
func reasonWords(r core.ViewReason) string {
	switch r {
	case core.NoDaemon:
		return "the job service is not answering"
	case core.StartFailed:
		return "the job service could not be started"
	case core.CLINotInstalled:
		return "vibesys-cli is not installed"
	case core.ClientTooOld:
		return "the job service is newer than this host"
	case core.DaemonTooOld:
		return "the job service is older than this host needs"
	case core.StreamLost:
		return "the link to the job service broke"
	case core.MessageTooLarge:
		return "the job service sent more than the link allows"
	case core.StoreOwnedElsewhere:
		return "the task's store is owned by another host"
	case core.StoreAway:
		return "the task's store is away"
	case core.Connecting:
		return "the first read has not finished"
	}
	return "unknown reason"
}

// clip cuts a failure to what the contract holds, on a character boundary.
func clip(s string) string {
	if len(s) <= maxFailureBytes {
		return s
	}
	cut := maxFailureBytes
	for cut > 0 && !isRuneStart(s[cut]) {
		cut--
	}
	return s[:cut]
}

func isRuneStart(b byte) bool { return b&0xC0 != 0x80 }

// readMessages returns every message of a run, oldest first, a page at a time.
func (e *Engine) readMessages(run core.RunID) ([]store.MessageRecord, error) {
	var out []store.MessageRecord
	for after := uint64(0); ; {
		page, err := e.Store.Messages(run, after, store.MaxPageMessages)
		if err != nil {
			return nil, fmt.Errorf("read the messages of %s: %w", run, err)
		}
		out = append(out, page...)
		if len(page) < store.MaxPageMessages {
			return out, nil
		}
		after = page[len(page)-1].Seq
	}
}
