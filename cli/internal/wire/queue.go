package wire

import (
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
