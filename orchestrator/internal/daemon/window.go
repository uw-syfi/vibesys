package daemon

import (
	"context"
	"errors"
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

// maxWindowTries is how many times the facts of a check-in are tried when a record the host keeps cannot be
// read. A viewer's next press is held until the last check-in is settled, so a store that never answers
// must end in a failed check-in and not in a press that never gets through.
const maxWindowTries = 5

// computeWindowFacts carries out a ComputeWindowFacts: it reads the records the facts are made of,
// works them out with window.Facts, and answers the core with them, or with the reason they cannot be
// worked out. The answer is what is frozen in the check-in, so an effect carried out twice, or after
// the check-in was settled, changes nothing: the core drops a second answer.
//
// What it reads that the core does not hold: the job log of the run's task (not the capped copy that
// clients are served), the run's finished turns and the run's messages, from the store. The job log of a
// run that is live is read afresh, after the window ended (see JobLog): the kept view follows a stream and
// can be behind it, and a result recorded inside the window that arrives after the facts froze would be in
// no window at all. A job log that cannot be read now fails the check-in rather than leaving its
// measurements and plans out of the facts, because a window that says nothing was measured is a different
// statement from a window the host could not look into. A store that cannot be read is tried again, and
// fails the check-in after maxWindowTries.
func (d *Daemon) computeWindowFacts(ctx context.Context, e core.ComputeWindowFacts) error {
	state := d.State()
	check, ok := state.CheckIn(e.CheckIn)
	if _, pending := check.Outcome.(core.CheckInPending); !ok || !pending {
		delete(d.windowTries, e.CheckIn)
		return nil
	}
	run, ok := state.Run(e.Run)
	if !ok {
		return fmt.Errorf("window facts of check-in %s: no run %s", e.CheckIn, e.Run)
	}
	view, why := d.jobLogOf(ctx, run)
	if why == "" {
		err := d.factsFrom(view, e, check)
		if err == nil {
			delete(d.windowTries, e.CheckIn)
			return nil
		}
		if d.windowTries[e.CheckIn]++; d.windowTries[e.CheckIn] < maxWindowTries {
			return err
		}
		why = clip(fmt.Sprintf("the host could not read its records for this window after %d tries: %v", maxWindowTries, err))
	}
	delete(d.windowTries, e.CheckIn)
	_, err := d.Apply(core.WindowFactsFailed{CheckIn: e.CheckIn, Detail: why})
	return err
}

// jobLogOf returns the job log the facts of a check-in are worked out from, or why it cannot be read.
func (d *Daemon) jobLogOf(ctx context.Context, run core.Run) (search.View, string) {
	// A run that is over is read once, after its end, and that read is its last (see the reconciler), so
	// the view held is complete for a window that ends with the run. A live run's is read now.
	if d.cfg.JobLog != nil && !run.Status.Final() {
		view, err := bound(d.cfg.Bounds, ctx, func(c context.Context) (search.View, error) { return d.cfg.JobLog.ReadJobLog(c, run) })
		switch {
		case errors.Is(err, ErrExpired):
			return search.View{}, "the job log of this run's task did not answer in time: try again later"
		case err != nil:
			return search.View{}, clip("the job log of this run's task cannot be read now (" + err.Error() + "): try again later")
		case view.Reason != core.Readable:
			return search.View{}, clip("the job log of this run's task cannot be read now (" + reasonWords(view.Reason) + "): try again later")
		}
		return view, ""
	}
	return d.readableView(run.ID)
}

// factsFrom works the facts of a check-in out from a job log and the run's other records, and answers
// the core with them or with the reason a window is too big to keep. An error is a record the host could
// not read, to be tried again.
func (d *Daemon) factsFrom(view search.View, e core.ComputeWindowFacts, check core.CheckIn) error {
	// The messages are read before the state, so that every check-in answer in the messages is linked in
	// the state: the state is at least as new.
	records, err := d.Engine.readMessages(e.Run)
	if err != nil {
		return err
	}
	state := d.State()
	ledger := &cost.Ledger{}
	for _, a := range state.Agents(e.Run) {
		ledger.Agent(a)
	}
	if err := d.Engine.readTurns(state, e.Run, ledger); err != nil {
		return err
	}
	// A catch-up answer is a report like any other, dated after the window it answers, and belongs to its
	// check-in: counting it again in the next window would tell the viewer of it twice.
	answers := map[core.MessageID]bool{}
	for _, c := range state.CheckIns(e.Run) {
		if c.Report != "" {
			answers[c.Report] = true
		}
	}
	messages := make([]core.Message, 0, len(records))
	for _, r := range records {
		if !answers[r.Message.ID] {
			messages = append(messages, r.Message)
		}
	}
	facts := window.Facts(window.Input{
		Run: e.Run, Since: e.Since, Until: e.Until, View: view, Ledger: ledger, Agents: state.Agents(e.Run), Messages: messages,
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
