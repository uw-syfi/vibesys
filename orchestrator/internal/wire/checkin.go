package wire

import (
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// checkInOut is a check-in as the contract states it. The outcome decides which of the facts and the
// failure it carries, so the contract's rule that only a ready check-in has facts holds by
// construction.
func checkInOut(c core.CheckIn) *pb.CheckIn {
	out := &pb.CheckIn{
		Id: string(c.ID), RunId: string(c.Run), Viewer: &pb.Viewer{Id: string(c.Viewer.ID), Label: c.Viewer.Label},
		Since: when(c.Since), At: when(c.At), AskMessageId: string(c.Ask), ReportMessageId: string(c.Report),
	}
	switch o := c.Outcome.(type) {
	case core.CheckInPending:
		out.State = pb.CheckInState_CHECK_IN_STATE_PENDING
	case core.CheckInReady:
		out.State, out.Facts = pb.CheckInState_CHECK_IN_STATE_READY, windowFactsOut(o.Facts)
	case core.CheckInFailed:
		out.State, out.Failure = pb.CheckInState_CHECK_IN_STATE_FAILED, o.Detail
	}
	return out
}

func checkInIn(c *pb.CheckIn, errs *errorList) core.CheckIn {
	out := core.CheckIn{
		ID: core.CheckInID(c.GetId()), Run: core.RunID(c.GetRunId()),
		Viewer: core.Viewer{ID: core.ViewerID(c.GetViewer().GetId()), Label: c.GetViewer().GetLabel()},
		Since:  moment(c.GetSince(), errs), At: moment(c.GetAt(), errs),
		Ask: core.MessageID(c.GetAskMessageId()), Report: core.MessageID(c.GetReportMessageId()),
	}
	switch c.GetState() {
	case pb.CheckInState_CHECK_IN_STATE_PENDING:
		out.Outcome = core.CheckInPending{}
	case pb.CheckInState_CHECK_IN_STATE_READY:
		out.Outcome = core.CheckInReady{Facts: windowFactsIn(c.GetFacts(), errs)}
	case pb.CheckInState_CHECK_IN_STATE_FAILED:
		out.Outcome = core.CheckInFailed{Detail: c.GetFailure()}
	default:
		errs.addf("no core outcome for check-in state %v", c.GetState())
	}
	return out
}

func windowFactsOut(f core.WindowFacts) *pb.WindowFacts {
	out := &pb.WindowFacts{
		Spend:         spendOut(f.Spend),
		AgentsStarted: f.AgentsStarted, AgentsEnded: f.AgentsEnded, JobsMeasured: f.JobsMeasured,
		PlansCreated: f.PlansCreated, PlansChanged: f.PlansChanged,
	}
	for _, m := range f.Objectives {
		out.Objectives = append(out.Objectives, &pb.ObjectiveMove{
			Metric: m.Metric, Direction: objectiveDirections.out(m.Direction), Start: bestOut(m.Start), End: bestOut(m.End),
		})
	}
	for _, id := range f.Reports {
		out.ReportMessageIds = append(out.ReportMessageIds, string(id))
	}
	return out
}

func windowFactsIn(f *pb.WindowFacts, errs *errorList) core.WindowFacts {
	out := core.WindowFacts{
		Spend:         spendIn(f.GetSpend(), errs),
		AgentsStarted: f.GetAgentsStarted(), AgentsEnded: f.GetAgentsEnded(), JobsMeasured: f.GetJobsMeasured(),
		PlansCreated: nilIfNone(f.GetPlansCreated()), PlansChanged: nilIfNone(f.GetPlansChanged()),
	}
	for _, m := range f.GetObjectives() {
		out.Objectives = append(out.Objectives, core.ObjectiveMove{
			Metric: m.GetMetric(), Direction: objectiveDirections.in(m.GetDirection(), errs), Start: bestIn(m.GetStart(), errs), End: bestIn(m.GetEnd(), errs),
		})
	}
	for _, id := range f.GetReportMessageIds() {
		out.Reports = append(out.Reports, core.MessageID(id))
	}
	return out
}

// nilIfNone reads an absent list and an empty one alike, as the core holds both.
func nilIfNone[T any](xs []T) []T {
	if len(xs) == 0 {
		return nil
	}
	return xs
}

func spendOut(s core.Spend) *pb.WindowSpend {
	out := &pb.WindowSpend{Turns: s.Turns, UnpricedTurns: s.Unpriced, Usage: usageOut(s.Usage)}
	for _, r := range s.Roles {
		out.Roles = append(out.Roles, &pb.RoleSpend{Role: roles.out(r.Role), Turns: r.Turns, UnpricedTurns: r.Unpriced, Usage: usageOut(r.Usage)})
	}
	return out
}

func spendIn(s *pb.WindowSpend, errs *errorList) core.Spend {
	out := core.Spend{Turns: s.GetTurns(), Unpriced: s.GetUnpricedTurns(), Usage: usageIn(s.GetUsage())}
	for _, r := range s.GetRoles() {
		out.Roles = append(out.Roles, core.RoleSpend{Role: roles.in(r.GetRole(), errs), Turns: r.GetTurns(), Unpriced: r.GetUnpricedTurns(), Usage: usageIn(r.GetUsage())})
	}
	return out
}

func bestOut(b *core.Best) *pb.BestValue {
	if b == nil {
		return nil
	}
	return &pb.BestValue{Value: b.Value, JobId: string(b.Job), Commit: string(b.Commit), At: timestamppb.New(b.At)}
}

func bestIn(b *pb.BestValue, errs *errorList) *core.Best {
	if b == nil {
		return nil
	}
	return &core.Best{Value: b.GetValue(), Job: core.JobID(b.GetJobId()), Commit: core.Commit(b.GetCommit()), At: moment(b.GetAt(), errs)}
}
