package main

import (
	"context"
	"fmt"
	"strings"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// queueCommand is `queue [--json]`: the task's queued and running jobs grouped by plan, for a lead
// deciding what goes next.
func queueCommand(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("queue", d)
	positional, err := parse(fs, args)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	if len(positional) != 0 {
		return fail(d, c.json, usagef("queue: unexpected argument %q", positional[0]))
	}
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.Queue(ctx, &cliv1.QueueRequest{Context: callCtx})
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
		return exitOK
	}
	fmt.Fprint(d.stdout, renderQueue(resp, callCtx.GetTask()))
	return exitOK
}

// renderQueue is the queue as text: a line for the drain, if any, then each plan's line and its
// jobs, one line each, with why a queued job waits and its notes under it.
func renderQueue(q *cliv1.QueueResponse, task string) string {
	var b strings.Builder
	if dr := q.GetDrain(); dr != nil {
		fmt.Fprintln(&b, describeDrain(dr))
	}
	if len(q.GetGroups()) == 0 {
		fmt.Fprintln(&b, "no queued or running job")
	}
	for _, g := range q.GetGroups() {
		if p := g.GetPlan(); p != nil {
			priority := priorityName(p.GetPriority())
			if p.GetPriority() == cliv1.Priority_PRIORITY_UNSPECIFIED {
				priority = "normal"
			}
			fmt.Fprintf(&b, "plan %d\t%s\tpriority %s\t%s\n", p.GetNumber(), planStatusName(p.GetStatus()), priority, p.GetTitle())
		} else {
			fmt.Fprintln(&b, "no plan")
		}
		for _, e := range g.GetJobs() {
			fmt.Fprintf(&b, "  %s\n", describeQueued(e, task))
		}
	}
	return b.String()
}

// describeQueued is a job's line in the queue and what goes under it.
func describeQueued(e *cliv1.QueuedJob, task string) string {
	j := e.GetJob()
	commit := j.GetSpec().GetCommit()
	if len(commit) > 12 {
		commit = commit[:12]
	}
	kind := "command"
	switch {
	case j.GetSpec().GetCommand().GetLab() != nil:
		kind = fmt.Sprintf("lab for %v", j.GetSpec().GetCommand().GetTimeout().AsDuration())
	case j.GetSpec().GetCheck() != nil:
		kind = "check"
	case j.GetSpec().GetMeasure() != nil:
		kind = fmt.Sprintf("measure (%d samples)", j.GetSpec().GetMeasure().GetSamples())
	}
	fields := []string{j.GetId(), stateName(j.GetState()), kind, commit}
	if j.GetState() == cliv1.JobState_JOB_STATE_RUNNING {
		if p := placementOf(j.GetEnvironment()); p != "" {
			fields = append(fields, "on "+p)
		}
	} else {
		fields = append(fields, "priority "+priorityName(e.GetPriority()))
	}
	if subs := j.GetSubmitters(); len(subs) > 0 && subs[0].GetAgent() != "" {
		fields = append(fields, "by "+subs[0].GetAgent())
	}
	fields = append(fields, "age "+roundDuration(e.GetAge().AsDuration()))
	if e.GetEstimate() != nil {
		fields = append(fields, fmt.Sprintf("est %s (of %d)", roundDuration(e.GetEstimate().AsDuration()), e.GetEstimatedFrom()))
	} else {
		fields = append(fields, "est unknown")
	}
	line := strings.Join(fields, "\t")
	if w := e.GetWait(); w != nil {
		line += "\n\t  waiting: " + describeWait(w, task)
	}
	for _, n := range j.GetNotes() {
		line += "\n\t  note: " + n.GetText()
	}
	return line
}

// roundDuration is a duration to the minute, or the second below one.
func roundDuration(d time.Duration) string {
	if d < time.Minute {
		return d.Round(time.Second).String()
	}
	return strings.TrimSuffix(d.Round(time.Minute).String(), "0s")
}
