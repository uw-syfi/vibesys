package wire

import (
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
)

// FromAllocation is a standing allocation as the contract states it. It sets no kind's detail:
// which kind's message the backend's words go in is the wiring's to say, and no wiring calls this
// yet.
func FromAllocation(a alloc.Allocation) *cliv1.Allocation {
	out := &cliv1.Allocation{
		Id:           string(a.ID),
		Placement:    a.Placement,
		Run:          string(a.Run),
		RequestedFor: durationpb.New(a.RequestedFor),
		RequestedAt:  timestamppb.New(a.RequestedAt),
	}
	switch p := a.Phase.(type) {
	case alloc.Pending:
		out.State = cliv1.AllocationState_ALLOCATION_STATE_PENDING
	case alloc.Running:
		out.State = cliv1.AllocationState_ALLOCATION_STATE_RUNNING
		fromLive(out, p.Live)
	case alloc.Draining:
		out.State = cliv1.AllocationState_ALLOCATION_STATE_DRAINING
		fromLive(out, p.Live)
	case alloc.Ended:
		out.State = cliv1.AllocationState_ALLOCATION_STATE_ENDED
		out.EndedAt = timestamppb.New(p.At)
		out.EndReason = fromEndReason(p.Reason)
		if !p.StartedAt.IsZero() {
			out.StartedAt = timestamppb.New(p.StartedAt)
		}
	}
	if o := a.Renewal; o != nil {
		out.Renewal = &cliv1.RenewalOrder{Lead: durationpb.New(o.Lead), SuccessorFor: durationpb.New(o.For)}
		switch u := o.Until.(type) {
		case alloc.UntilTime:
			out.Renewal.Until = &cliv1.RenewalOrder_At{At: timestamppb.New(u.At)}
		case alloc.UntilRunEnd:
			out.Renewal.Until = &cliv1.RenewalOrder_RunEnd{RunEnd: true}
		}
	}
	if a.RenewedBy != "" {
		out.RenewedBy = proto.String(string(a.RenewedBy))
	}
	if a.RenewalSkipped != "" {
		out.RenewalSkippedReason = proto.String(string(a.RenewalSkipped))
	}
	return out
}

func fromLive(out *cliv1.Allocation, l alloc.Live) {
	out.StartedAt = timestamppb.New(l.StartedAt)
	if !l.EndsAt.IsZero() {
		out.EndsAt = timestamppb.New(l.EndsAt)
	}
	if l.Current != nil {
		out.RunningJob = proto.String(string(l.Current.Job))
	}
	for _, q := range l.Queue {
		out.QueuedJobs = append(out.QueuedJobs, string(q.Job))
	}
}

func fromEndReason(r alloc.EndReason) cliv1.AllocationEndReason {
	switch r {
	case alloc.EndRequested:
		return cliv1.AllocationEndReason_ALLOCATION_END_REASON_REQUESTED
	case alloc.EndTimeLimit:
		return cliv1.AllocationEndReason_ALLOCATION_END_REASON_TIME_LIMIT
	case alloc.EndQueueTimeout:
		return cliv1.AllocationEndReason_ALLOCATION_END_REASON_QUEUE_TIMEOUT
	case alloc.EndExternal:
		return cliv1.AllocationEndReason_ALLOCATION_END_REASON_EXTERNAL
	}
	return cliv1.AllocationEndReason_ALLOCATION_END_REASON_UNSPECIFIED
}
