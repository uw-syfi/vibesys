package daemon

import (
	"time"

	"google.golang.org/protobuf/types/known/durationpb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/setup"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// Detail bounds, from setup.proto.
const (
	maxCheckDetail = 1024
	maxDraftDetail = 2048
	maxUnitBytes   = 64
)

const (
	detailNotNamed = "the task is not named yet"
	detailReading  = "reading the task"
)

// hadSetup says the run is in setup or went through it, which is when its summary carries a SetupView.
func hadSetup(run core.Run) bool { return !run.Phase.Optimizing() || !run.OptimizeStartedAt.IsZero() }

// setupView works out the SetupView of a run. It is called with the engine's lock held and reads the
// observation from memory: it makes no call and starts no read. It returns nil for a run that never
// had a setup.
func (e *Engine) setupView(run core.Run) *pb.SetupView {
	if !hadSetup(run) {
		return nil
	}
	if run.Phase.Optimizing() {
		return finishedSetupView(run)
	}
	until := e.Clock.Now()
	if !run.EndedAt.IsZero() {
		until = run.EndedAt
	}
	view := &pb.SetupView{Task: run.SetupTask, Elapsed: durationpb.New(setupElapsed(run, until))}
	if run.Usage.CostUSD != nil {
		view.SpentUsd = *run.Usage.CostUSD
	}
	var obs setup.Observation
	if e.Setup != nil && run.SetupTask != "" {
		// An observation of another name, from before the lead renamed the task, says nothing of this one.
		if held, ok := e.Setup.Get(run.ID); ok && held.Task == run.SetupTask {
			obs = held
		}
	}
	result := obs.Derived(setup.Standard)
	if run.SetupTask == "" {
		result = setup.Unreadable(detailNotNamed)
	}
	view.Checks = checkMessages(result)
	if obs.Readable {
		view.Draft = &pb.SetupView_Card{Card: wire.TaskCardMessage(&obs.Facts.Card)}
	} else {
		detail := obs.Unreadable
		switch {
		case run.SetupTask == "":
			detail = detailNotNamed
		case detail == "":
			detail = detailReading
		}
		view.Draft = &pb.SetupView_Unreadable{Unreadable: &pb.SetupDraftUnreadable{Detail: clipBytes(detail, maxDraftDetail)}}
	}
	if b := result.Baseline; b != nil {
		view.BaselineValue, view.BaselineUnit, view.BaselineSpreadPercent = &b.Value, clipBytes(b.Unit, maxUnitBytes), &b.SpreadPercent
	}
	view.ReadFailure = clipBytes(obs.Failure, maxCheckDetail)
	return view
}

// finishedSetupView is the setup of a run that left it: every check passed, since the run could not
// leave otherwise, the card the run holds, and the spend and time of setup, which are over.
func finishedSetupView(run core.Run) *pb.SetupView {
	view := &pb.SetupView{Task: run.Task, Elapsed: durationpb.New(setupElapsed(run, run.OptimizeStartedAt))}
	if run.TaskCard != nil {
		view.Draft = &pb.SetupView_Card{Card: wire.TaskCardMessage(run.TaskCard)}
	}
	if run.SetupUsage != nil && run.SetupUsage.CostUSD != nil {
		view.SpentUsd = *run.SetupUsage.CostUSD
	}
	for _, id := range setup.Order {
		view.Checks = append(view.Checks, &pb.SetupCheck{Id: wire.SetupCheckIDMessage(id), State: pb.SetupCheckState_SETUP_CHECK_STATE_PASSED})
	}
	return view
}

// setupElapsed is how long setup lasted until the given time, in whole seconds so that a busy chat
// resends a summary at most once a second.
func setupElapsed(run core.Run, until time.Time) time.Duration {
	return max(until.Sub(run.CreatedAt), 0).Truncate(time.Second)
}

func checkMessages(r setup.Result) []*pb.SetupCheck {
	out := make([]*pb.SetupCheck, 0, len(r.Checks))
	for _, c := range r.Checks {
		out = append(out, &pb.SetupCheck{Id: wire.SetupCheckIDMessage(c.ID), State: checkState(c.State), Detail: clipBytes(c.Detail, maxCheckDetail)})
	}
	return out
}

func checkState(s setup.State) pb.SetupCheckState {
	switch s {
	case setup.Running:
		return pb.SetupCheckState_SETUP_CHECK_STATE_RUNNING
	case setup.Passed:
		return pb.SetupCheckState_SETUP_CHECK_STATE_PASSED
	case setup.Failed:
		return pb.SetupCheckState_SETUP_CHECK_STATE_FAILED
	default:
		return pb.SetupCheckState_SETUP_CHECK_STATE_PENDING
	}
}

// clipBytes cuts s to at most n bytes at a rune boundary.
func clipBytes(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && s[n]&0xC0 == 0x80 {
		n--
	}
	return s[:n]
}
