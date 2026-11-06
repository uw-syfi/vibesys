package wire

import (
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// waitKinds pairs each domain kind of wait with its contract value.
var waitKinds = []struct {
	domain core.WaitKind
	wire   cliv1.WaitKind
}{
	{core.WaitFull, cliv1.WaitKind_WAIT_KIND_FULL},
	{core.WaitHeldExclusively, cliv1.WaitKind_WAIT_KIND_HELD_EXCLUSIVELY},
	{core.WaitInUse, cliv1.WaitKind_WAIT_KIND_IN_USE},
	{core.WaitBehind, cliv1.WaitKind_WAIT_KIND_BEHIND},
	{core.WaitHeld, cliv1.WaitKind_WAIT_KIND_HELD},
	{core.WaitTrouble, cliv1.WaitKind_WAIT_KIND_TROUBLE},
	{core.WaitLeased, cliv1.WaitKind_WAIT_KIND_LEASED},
}

// WaitKind returns the contract's value for a kind of wait, or unspecified for one it does not name.
func WaitKind(k core.WaitKind) cliv1.WaitKind {
	for _, p := range waitKinds {
		if p.domain == k {
			return p.wire
		}
	}
	return cliv1.WaitKind_WAIT_KIND_UNSPECIFIED
}

// WaitReason returns the contract's message for why a job waits.
func WaitReason(w core.JobWait) *cliv1.WaitReason {
	out := &cliv1.WaitReason{Kind: WaitKind(w.Kind), Resource: w.Resource, Detail: w.Detail}
	for _, h := range w.Holders {
		out.Holders = append(out.Holders, &cliv1.WaitHolder{JobId: string(h.Job), Task: string(h.Task), Kind: JobKind(h.Kind), RunId: string(h.Run)})
	}
	if w.Elsewhere != nil {
		out.Elsewhere = LeaseHolder(*w.Elsewhere)
	}
	return out
}

// LeaseHolder returns the contract's message for who holds a lease.
func LeaseHolder(h core.LeaseHolder) *cliv1.LeaseHolder {
	out := &cliv1.LeaseHolder{User: h.User, Host: h.Host, JobId: string(h.Job)}
	if h.PID > 0 {
		out.Pid = uint32(h.PID)
	}
	if !h.Since.IsZero() {
		out.Since = timestamppb.New(h.Since)
	}
	return out
}

// QueueResponse returns the contract's message for a task's queue.
func QueueResponse(view core.QueueView, waits map[core.JobID]core.JobWait, drain *core.Drain) *cliv1.QueueResponse {
	out := &cliv1.QueueResponse{}
	if drain != nil {
		out.Drain = DaemonDrain(*drain)
	}
	for _, g := range view.Groups {
		group := &cliv1.QueueGroup{}
		if g.Plan != nil {
			group.Plan = Plan(*g.Plan)
		}
		for _, q := range g.Jobs {
			e := &cliv1.QueuedJob{Job: Job(q.Job), Priority: Priority(q.Priority), Age: durationpb.New(q.Waited)}
			if w, ok := waits[q.Job.ID]; ok && q.Job.State == core.JobQueued {
				e.Wait = WaitReason(w)
			}
			if q.Finished > 0 {
				e.Estimate, e.EstimatedFrom = durationpb.New(q.Estimate), uint32(q.Finished)
			}
			group.Jobs = append(group.Jobs, e)
		}
		out.Groups = append(out.Groups, group)
	}
	return out
}

// JobWaits returns the contract's messages for why jobs wait, in the order of the jobs.
func JobWaits(jobs []core.Job, waits map[core.JobID]core.JobWait) []*cliv1.JobWait {
	var out []*cliv1.JobWait
	for _, j := range jobs {
		if w, ok := waits[j.ID]; ok {
			out = append(out, &cliv1.JobWait{JobId: string(j.ID), Reason: WaitReason(w)})
		}
	}
	return out
}
