package wire

import (
	"fmt"
	"strconv"

	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// FromAllocationInput returns the contract's message for one input of the allocation machine, as
// the allocation log keeps it. It fails for an input this build has no message for, which no
// machine makes.
func FromAllocationInput(in alloc.Input) (*cliv1.AllocationInput, error) {
	out := &cliv1.AllocationInput{At: timestamppb.New(in.At)}
	switch e := in.Event.(type) {
	case alloc.Ask:
		out.Event = &cliv1.AllocationInput_Ask{Ask: &cliv1.AllocationAsked{Run: string(e.Run), Placement: e.Placement, RequestedFor: durationpb.New(e.For)}}
	case alloc.EndAsk:
		out.Event = &cliv1.AllocationInput_EndAsk{EndAsk: &cliv1.AllocationEndAsked{Run: string(e.Run), Id: string(e.ID), AfterJobs: e.AfterJobs}}
	case alloc.RenewAsk:
		out.Event = &cliv1.AllocationInput_RenewAsk{RenewAsk: &cliv1.AllocationRenewAsked{Run: string(e.Run), Id: string(e.ID), Order: FromRenewalOrder(e.Order)}}
	case alloc.RenewOffAsk:
		out.Event = &cliv1.AllocationInput_RenewOffAsk{RenewOffAsk: &cliv1.AllocationRenewOffAsked{Run: string(e.Run), Id: string(e.ID)}}
	case alloc.RunEnded:
		out.Event = &cliv1.AllocationInput_RunEnded{RunEnded: &cliv1.AllocationRunEnded{Run: string(e.Run)}}
	case alloc.JobSubmitted:
		out.Event = &cliv1.AllocationInput_JobSubmitted{JobSubmitted: &cliv1.AllocationJobSubmitted{
			Run: string(e.Run), Job: string(e.Job), Bound: durationpb.New(e.Bound), Into: string(e.Into), Eligible: append([]string(nil), e.Eligible...),
		}}
	case alloc.JobStarted:
		out.Event = &cliv1.AllocationInput_JobStarted{JobStarted: &cliv1.AllocationJobStarted{Job: string(e.Job)}}
	case alloc.JobFinished:
		out.Event = &cliv1.AllocationInput_JobFinished{JobFinished: &cliv1.AllocationJobFinished{Job: string(e.Job)}}
	case alloc.Submitted:
		out.Event = &cliv1.AllocationInput_Submitted{Submitted: &cliv1.AllocationBackendTook{Id: string(e.ID), Ref: e.Handle.Ref}}
	case alloc.SubmitRefused:
		out.Event = &cliv1.AllocationInput_SubmitRefused{SubmitRefused: &cliv1.AllocationBackendRefused{Id: string(e.ID), Detail: e.Detail}}
	case alloc.Observed:
		observed, err := fromObservation(e)
		if err != nil {
			return nil, err
		}
		out.Event = &cliv1.AllocationInput_Observed{Observed: observed}
	case alloc.Unreachable:
		out.Event = &cliv1.AllocationInput_Unreachable{Unreachable: &cliv1.AllocationUnreachable{Id: string(e.ID), Detail: e.Detail}}
	case alloc.Released:
		out.Event = &cliv1.AllocationInput_Released{Released: &cliv1.AllocationReleased{Id: string(e.ID)}}
	case alloc.Tick:
		out.Event = &cliv1.AllocationInput_Tick{Tick: &cliv1.AllocationTick{}}
	default:
		return nil, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("the allocation log has no message for an input of type %T", in.Event)}
	}
	return out, nil
}

func fromObservation(e alloc.Observed) (*cliv1.AllocationObserved, error) {
	out := &cliv1.AllocationObserved{Id: string(e.ID)}
	switch s := e.Seen.(type) {
	case alloc.SeenPending:
		out.Seen = &cliv1.AllocationObserved_Pending{Pending: &cliv1.SeenPending{Reason: s.Reason, EstimatedStart: timestamp(s.EstimatedStart)}}
	case alloc.SeenRunning:
		out.Seen = &cliv1.AllocationObserved_Running{Running: &cliv1.SeenRunning{Node: s.Node, StartedAt: timestamp(s.StartedAt), EndsAt: timestamp(s.EndsAt)}}
	case alloc.SeenEnded:
		cause := cliv1.SeenEndedCause_SEEN_ENDED_CAUSE_OTHERWISE
		if s.Cause == alloc.EndedByTimeLimit {
			cause = cliv1.SeenEndedCause_SEEN_ENDED_CAUSE_TIME_LIMIT
		}
		out.Seen = &cliv1.AllocationObserved_Ended{Ended: &cliv1.SeenEnded{Cause: cause, At: timestamp(s.At)}}
	default:
		return nil, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("the allocation log has no message for an observation of type %T", e.Seen)}
	}
	return out, nil
}

// ToAllocationInput returns the input a logged message stands for, or says what is wrong with it.
func ToAllocationInput(m *cliv1.AllocationInput) (alloc.Input, error) {
	in := alloc.Input{At: toTime(m.GetAt())}
	switch e := m.GetEvent().(type) {
	case *cliv1.AllocationInput_Ask:
		in.Event = alloc.Ask{Run: core.RunID(e.Ask.GetRun()), Placement: e.Ask.GetPlacement(), For: e.Ask.GetRequestedFor().AsDuration()}
	case *cliv1.AllocationInput_EndAsk:
		in.Event = alloc.EndAsk{Run: core.RunID(e.EndAsk.GetRun()), ID: alloc.ID(e.EndAsk.GetId()), AfterJobs: e.EndAsk.GetAfterJobs()}
	case *cliv1.AllocationInput_RenewAsk:
		order, err := ToRenewalOrder(e.RenewAsk.GetOrder())
		if err != nil {
			return alloc.Input{}, err
		}
		in.Event = alloc.RenewAsk{Run: core.RunID(e.RenewAsk.GetRun()), ID: alloc.ID(e.RenewAsk.GetId()), Order: order}
	case *cliv1.AllocationInput_RenewOffAsk:
		in.Event = alloc.RenewOffAsk{Run: core.RunID(e.RenewOffAsk.GetRun()), ID: alloc.ID(e.RenewOffAsk.GetId())}
	case *cliv1.AllocationInput_RunEnded:
		in.Event = alloc.RunEnded{Run: core.RunID(e.RunEnded.GetRun())}
	case *cliv1.AllocationInput_JobSubmitted:
		j := e.JobSubmitted
		in.Event = alloc.JobSubmitted{
			Run: core.RunID(j.GetRun()), Job: core.JobID(j.GetJob()), Bound: j.GetBound().AsDuration(),
			Into: alloc.ID(j.GetInto()), Eligible: append([]string(nil), j.GetEligible()...),
		}
	case *cliv1.AllocationInput_JobStarted:
		in.Event = alloc.JobStarted{Job: core.JobID(e.JobStarted.GetJob())}
	case *cliv1.AllocationInput_JobFinished:
		in.Event = alloc.JobFinished{Job: core.JobID(e.JobFinished.GetJob())}
	case *cliv1.AllocationInput_Submitted:
		id := alloc.ID(e.Submitted.GetId())
		in.Event = alloc.Submitted{ID: id, Handle: alloc.Handle{Alloc: id, Ref: e.Submitted.GetRef()}}
	case *cliv1.AllocationInput_SubmitRefused:
		in.Event = alloc.SubmitRefused{ID: alloc.ID(e.SubmitRefused.GetId()), Detail: e.SubmitRefused.GetDetail()}
	case *cliv1.AllocationInput_Observed:
		seen, err := toObservation(e.Observed)
		if err != nil {
			return alloc.Input{}, err
		}
		in.Event = alloc.Observed{ID: alloc.ID(e.Observed.GetId()), Seen: seen}
	case *cliv1.AllocationInput_Unreachable:
		in.Event = alloc.Unreachable{ID: alloc.ID(e.Unreachable.GetId()), Detail: e.Unreachable.GetDetail()}
	case *cliv1.AllocationInput_Released:
		in.Event = alloc.Released{ID: alloc.ID(e.Released.GetId())}
	case *cliv1.AllocationInput_Tick:
		in.Event = alloc.Tick{}
	default:
		return alloc.Input{}, invalid("input.event: no event")
	}
	return in, nil
}

func toObservation(o *cliv1.AllocationObserved) (alloc.Observation, error) {
	switch s := o.GetSeen().(type) {
	case *cliv1.AllocationObserved_Pending:
		return alloc.SeenPending{Reason: s.Pending.GetReason(), EstimatedStart: toTime(s.Pending.GetEstimatedStart())}, nil
	case *cliv1.AllocationObserved_Running:
		return alloc.SeenRunning{Node: s.Running.GetNode(), StartedAt: toTime(s.Running.GetStartedAt()), EndsAt: toTime(s.Running.GetEndsAt())}, nil
	case *cliv1.AllocationObserved_Ended:
		cause := alloc.EndedOtherwise
		switch s.Ended.GetCause() {
		case cliv1.SeenEndedCause_SEEN_ENDED_CAUSE_TIME_LIMIT:
			cause = alloc.EndedByTimeLimit
		case cliv1.SeenEndedCause_SEEN_ENDED_CAUSE_OTHERWISE:
		case cliv1.SeenEndedCause_SEEN_ENDED_CAUSE_UNSPECIFIED:
			return nil, invalid("observed.ended.cause: unspecified")
		}
		return alloc.SeenEnded{Cause: cause, At: toTime(s.Ended.GetAt())}, nil
	}
	return nil, invalid("observed.seen: nothing seen")
}

// FromRenewalOrder returns the contract's message for a renewal order.
func FromRenewalOrder(o alloc.RenewalOrder) *cliv1.RenewalOrder {
	out := &cliv1.RenewalOrder{Lead: durationpb.New(o.Lead), SuccessorFor: durationpb.New(o.For)}
	switch u := o.Until.(type) {
	case alloc.UntilTime:
		out.Until = &cliv1.RenewalOrder_At{At: timestamppb.New(u.At)}
	case alloc.UntilRunEnd:
		out.Until = &cliv1.RenewalOrder_RunEnd{RunEnd: true}
	}
	return out
}

// ToRenewalOrder returns the domain order for the contract's message.
func ToRenewalOrder(o *cliv1.RenewalOrder) (alloc.RenewalOrder, error) {
	out := alloc.RenewalOrder{Lead: o.GetLead().AsDuration(), For: o.GetSuccessorFor().AsDuration()}
	switch u := o.GetUntil().(type) {
	case *cliv1.RenewalOrder_At:
		out.Until = alloc.UntilTime{At: toTime(u.At)}
	case *cliv1.RenewalOrder_RunEnd:
		out.Until = alloc.UntilRunEnd{}
	default:
		return alloc.RenewalOrder{}, invalid("renewal.until: say when the order stops")
	}
	return out, nil
}

// AllocationView is an allocation with what the daemon knows of it besides its record: the backend's
// name for it, and whether its placement's allocations are batch jobs of a Slurm cluster, whose
// words the wire carries as the allocation's detail.
type AllocationView struct {
	alloc.Allocation
	// Slurm says the allocation is a batch job of a Slurm cluster.
	Slurm bool
}

// FromAllocationView returns the contract's message for an allocation, with the Slurm detail the
// backend's observation gave when the placement's allocations are batch jobs: the batch job's id
// when it is a number, why it waits and when it is expected to start, and the node it runs on.
func FromAllocationView(v AllocationView) *cliv1.Allocation {
	out := FromAllocation(v.Allocation)
	if !v.Slurm {
		return out
	}
	d := &cliv1.SlurmAllocation{}
	if _, err := strconv.ParseUint(v.Handle.Ref, 10, 64); err == nil {
		d.JobId = v.Handle.Ref
	}
	switch p := v.Phase.(type) {
	case alloc.Pending:
		d.PendingReason = p.Reason
		d.EstimatedStart = timestamp(p.EstimatedStart)
	case alloc.Running:
		d.Node = p.Node
	case alloc.Draining:
		d.Node = p.Node
	case alloc.Ended:
	}
	if d.JobId == "" {
		return out
	}
	out.Detail = &cliv1.Allocation_Slurm{Slurm: d}
	return out
}
