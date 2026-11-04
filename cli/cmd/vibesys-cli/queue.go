package main

import (
	"fmt"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// shownHolders is how many of the jobs in a wait's way a line names.
const shownHolders = 3

// describeWait is why a queued job waits, in words: what it waits for, and the jobs in its way, a
// job of another task named with its task.
func describeWait(w *cliv1.WaitReason, task string) string {
	var names []string
	for i, h := range w.GetHolders() {
		if i == shownHolders {
			names = append(names, fmt.Sprintf("%d more", len(w.GetHolders())-shownHolders))
			break
		}
		names = append(names, describeHolder(h, task))
	}
	holders := strings.Join(names, ", ")
	switch w.GetKind() {
	case cliv1.WaitKind_WAIT_KIND_FULL:
		return fmt.Sprintf("%s is full, held by %s", w.GetResource(), holders)
	case cliv1.WaitKind_WAIT_KIND_HELD_EXCLUSIVELY:
		return fmt.Sprintf("%s held exclusively by %s", w.GetResource(), holders)
	case cliv1.WaitKind_WAIT_KIND_IN_USE:
		return fmt.Sprintf("needs %s alone, in use by %s", w.GetResource(), holders)
	case cliv1.WaitKind_WAIT_KIND_BEHIND:
		return fmt.Sprintf("behind %s, which waits for %s", holders, w.GetResource())
	case cliv1.WaitKind_WAIT_KIND_HELD, cliv1.WaitKind_WAIT_KIND_TROUBLE:
		return w.GetDetail()
	case cliv1.WaitKind_WAIT_KIND_UNSPECIFIED:
	}
	// A kind a newer daemon added: say what is known of it.
	var known []string
	for _, part := range []string{w.GetResource(), holders, w.GetDetail()} {
		if part != "" {
			known = append(known, part)
		}
	}
	return strings.Join(known, ", ")
}

// describeHolder names a job in another's way: "j41 (measure, run B)", with "of task t" for a job
// of another task than the one listed.
func describeHolder(h *cliv1.WaitHolder, task string) string {
	name := h.GetJobId()
	if h.GetTask() != "" && h.GetTask() != task {
		name += " of task " + h.GetTask()
	}
	var facts []string
	if h.GetKind() != cliv1.JobKind_JOB_KIND_UNSPECIFIED {
		facts = append(facts, lower("JOB_KIND_", h.GetKind().String()))
	}
	if h.GetRunId() != "" {
		facts = append(facts, "run "+h.GetRunId())
	}
	if len(facts) > 0 {
		name += " (" + strings.Join(facts, ", ") + ")"
	}
	return name
}
