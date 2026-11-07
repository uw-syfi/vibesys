package daemon_test

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

// jobLogRig is a rig whose host reads a live run's job log afresh from log, while the view it keeps for
// serving (searches) follows the log only when a test lets it.
func jobLogRig(t *testing.T) (*daemontest.Rig, *daemontest.JobLog, *search.Store, core.Run, core.Agent) {
	t.Helper()
	rig := daemontest.NewRig(t)
	log, searches := &daemontest.JobLog{}, new(search.Store)
	rig.JobLog, rig.Search = log, searches
	rig.Restart(t)
	run, lead := beginRun(t, rig)
	return rig, log, searches, run, lead
}

// Every result is in exactly the window its record time says, however late the host's kept view of the job
// log catches up: results recorded before a press may reach the kept view after the check-in's facts
// froze, or never, and a window is worked out from a read made after it ended. The reference is the
// truth the job service holds, counted by the half-open window; the arrival order of results to the kept
// view, the order of presses, records and settling are drawn.
func TestEveryResultIsCountedInItsWindowWhateverReachesTheKeptViewWhen(t *testing.T) {
	for seed := range uint64(40) {
		r := rand.New(rand.NewPCG(seed, 17))
		rig, log, searches, run, _ := jobLogRig(t)
		var recorded []time.Time
		for step := range 40 {
			switch r.IntN(5) {
			case 0, 1:
				rig.Clock.Advance(time.Duration(1+r.IntN(30)) * time.Second)
				at := rig.Clock.Now()
				log.Record(measure(fmt.Sprint("j", step), run.ID, at, "tput", float64(step))...)
				recorded = append(recorded, at)
			case 2:
				// The kept view catches up, sometimes.
				if r.IntN(2) == 0 {
					searches.Put(run.ID, log.Held())
				}
			case 3:
				rig.Clock.Advance(time.Duration(r.IntN(10)) * time.Second)
				if _, err := press(t, viewing(ann), rig, run.ID); err != nil {
					t.Fatal(err)
				}
			default:
				rig.Settle(t)
			}
		}
		rig.Settle(t)
		counted := 0
		for _, c := range rig.Daemon.State().CheckIns(run.ID) {
			got := ready(t, c).JobsMeasured
			want := 0
			for _, at := range recorded {
				if !at.Before(c.Since) && at.Before(c.At) {
					want++
				}
			}
			if int(got) != want {
				t.Fatalf("seed %d: the window %v to %v counts %d measurements, the job service recorded %d in it (all: %v)", seed, c.Since, c.At, got, want, recorded)
			}
			counted += want
		}
		// The windows tile, so nothing recorded before the last window's end is in no window.
		if cs := rig.Daemon.State().CheckIns(run.ID); len(cs) > 0 {
			end := cs[len(cs)-1].At
			all := 0
			for _, at := range recorded {
				if at.Before(end) {
					all++
				}
			}
			if counted != all {
				t.Fatalf("seed %d: the windows count %d measurements, %d were recorded before the last one ended", seed, counted, all)
			}
		}
	}
}

// A job log that cannot be read at the moment of the press fails the check-in, with the reason in words,
// and the viewer's next press covers the same window.
func TestACatchUpFailsWhenTheFreshReadOfTheJobLogFails(t *testing.T) {
	for _, tc := range []struct {
		name string
		set  func(*daemontest.JobLog)
		why  string
	}{
		{"the read errors", func(l *daemontest.JobLog) { l.Err = errors.New("link broke") }, "link broke"},
		{"the store is away", func(l *daemontest.JobLog) { l.Reason = core.StoreAway }, "the task's store is away"},
		{"the service is not answering", func(l *daemontest.JobLog) { l.Reason = core.NoDaemon }, "not answering"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rig, log, _, run, _ := jobLogRig(t)
			tc.set(log)
			rig.Clock.Advance(time.Minute)
			receipt, err := press(t, viewing(ann), rig, run.ID)
			if err != nil {
				t.Fatal(err)
			}
			rig.Settle(t)
			failed, ok := checkIn(t, rig, receipt.GetCheckInId()).Outcome.(core.CheckInFailed)
			if !ok || !strings.Contains(failed.Detail, tc.why) {
				t.Fatalf("outcome %+v, want a failure that says %q", checkIn(t, rig, receipt.GetCheckInId()).Outcome, tc.why)
			}
			log.Err, log.Reason = nil, core.Readable
			rig.Clock.Advance(time.Minute)
			again, err := press(t, viewing(ann), rig, run.ID)
			if err != nil {
				t.Fatal(err)
			}
			rig.Settle(t)
			if c := checkIn(t, rig, again.GetCheckInId()); !c.Since.Equal(run.CreatedAt) {
				t.Fatalf("the next window starts at %v, not where the failed one did (%v)", c.Since, run.CreatedAt)
			}
		})
	}
}

// settleThroughErrors steps the daemon until a step moves nothing and fails nothing: an effect that cannot
// be carried out is tried again on the next step, and the step says so.
func settleThroughErrors(t *testing.T, rig *daemontest.Rig) (failedSteps int) {
	t.Helper()
	for range 10000 {
		rig.Hub.Flush()
		moved, err := rig.Daemon.Step(context.Background())
		if err != nil {
			failedSteps++
			continue
		}
		if !moved {
			return failedSteps
		}
	}
	t.Fatal("the daemon never settled")
	return failedSteps
}

// A record the host keeps that cannot be read is tried a few times and then fails the check-in: the
// viewer's next press is held until the last check-in settles, so a store that never answers must not hold
// it for ever.
func TestACatchUpWhoseRecordsCannotBeReadFailsAfterAFewTries(t *testing.T) {
	rig, _, _, run, _ := jobLogRig(t)
	rig.Faults.FailMessages = -1
	rig.Clock.Advance(time.Minute)
	receipt, err := press(t, viewing(ann), rig, run.ID)
	if err != nil {
		t.Fatal(err)
	}
	if tries := settleThroughErrors(t, rig); tries != 4 {
		t.Fatalf("the facts were tried %d times and failed before the check-in did, want the 4 the bound allows", tries+1)
	}
	failed, ok := checkIn(t, rig, receipt.GetCheckInId()).Outcome.(core.CheckInFailed)
	if !ok || !strings.Contains(failed.Detail, "could not read its records") {
		t.Fatalf("outcome %+v, want a failure that says the records could not be read", checkIn(t, rig, receipt.GetCheckInId()).Outcome)
	}
	// The store answers again, and the viewer is not held: the next press is a new check-in from the same start.
	rig.Faults.FailMessages = 0
	rig.Clock.Advance(time.Minute)
	again, err := press(t, viewing(ann), rig, run.ID)
	if err != nil || again.GetCheckInId() == receipt.GetCheckInId() {
		t.Fatalf("the next press: %v, %v", again, err)
	}
	rig.Settle(t)
	if c := checkIn(t, rig, again.GetCheckInId()); !c.Since.Equal(run.CreatedAt) {
		t.Fatalf("the next window starts at %v, want the run's start", c.Since)
	}
	if _, ready := checkIn(t, rig, again.GetCheckInId()).Outcome.(core.CheckInReady); !ready {
		t.Fatalf("outcome %+v", checkIn(t, rig, again.GetCheckInId()).Outcome)
	}
}

// A read that fails for a while and then works is not a failure: it is tried again, and the check-in
// is made from the records.
func TestACatchUpWhoseRecordsAreBackWithinTheTriesIsMade(t *testing.T) {
	for failures := 1; failures < 5; failures++ {
		rig, _, _, run, _ := jobLogRig(t)
		rig.Faults.FailMessages = failures
		rig.Clock.Advance(time.Minute)
		receipt, err := press(t, viewing(ann), rig, run.ID)
		if err != nil {
			t.Fatal(err)
		}
		if tries := settleThroughErrors(t, rig); tries != failures {
			t.Fatalf("%d failed reads made %d failed steps", failures, tries)
		}
		if _, ok := checkIn(t, rig, receipt.GetCheckInId()).Outcome.(core.CheckInReady); !ok {
			t.Fatalf("after %d failed reads the check-in is %+v", failures, checkIn(t, rig, receipt.GetCheckInId()).Outcome)
		}
	}
}

// The answer to a catch-up request is a report the lead posts after the window it answers, and the next
// window must not count it: it belongs to its own check-in. A report the lead posts that answers nothing is
// an ordinary one, and is counted where it falls.
func TestACatchUpAnswerIsNotCountedAgainInTheNextWindow(t *testing.T) {
	rig, _, _, run, lead := jobLogRig(t)
	// The lead's turns stay open until a test ends them, so the report is posted inside the request's turn.
	rig.Sidecars.React = func(s *daemontest.Session, cmd *pb.SessionCommand) {
		if _, start := cmd.GetBody().(*pb.SessionCommand_StartTurn); !start {
			daemontest.DefaultReaction(s, cmd)
		}
	}
	post := func(id, headline string) core.MessageID {
		t.Helper()
		in := core.Command{ID: core.CommandID(id), Digest: core.Digest(fmt.Sprintf("%064x", len(id))), Caller: core.AgentCaller{Agent: lead.ID},
			Body: core.PostReport{Report: core.Report{Kind: core.ReportProgress, Headline: headline}}}
		if _, err := rig.Daemon.Apply(in); err != nil {
			t.Fatal(err)
		}
		receipt, seen := rig.Daemon.State().Recall(in.ID, in.Digest)
		if seen != core.RecallRepeat || receipt.Refusal != "" {
			t.Fatalf("report %s: %+v", id, receipt)
		}
		return receipt.Message
	}

	rig.Clock.Advance(time.Minute)
	first, err := press(t, viewing(ann), rig, run.ID)
	if err != nil {
		t.Fatal(err)
	}
	rig.Settle(t)
	asked := checkIn(t, rig, first.GetCheckInId())
	if asked.Ask == "" {
		t.Fatalf("the lead was not asked: %+v", asked)
	}
	rig.Clock.Advance(time.Minute)
	answer := post("answer", "since you looked")
	if got := checkIn(t, rig, first.GetCheckInId()).Report; got != answer {
		t.Fatalf("the report %s did not answer the request: the check-in links %q", answer, got)
	}
	rig.Clock.Advance(time.Minute)
	other := post("other", "a second report in the same turn answers nothing")
	rig.Clock.Advance(time.Minute)
	second, err := press(t, viewing(ann), rig, run.ID)
	if err != nil {
		t.Fatal(err)
	}
	rig.Settle(t)
	got := ready(t, checkIn(t, rig, second.GetCheckInId())).Reports
	if !slices.Equal(got, []core.MessageID{other}) {
		t.Fatalf("the second window counts the reports %v, want only %s: the first check-in's answer is %s", got, other, answer)
	}
}
