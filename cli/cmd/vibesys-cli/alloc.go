package main

import (
	"context"
	"flag"
	"fmt"
	"strings"
	"time"

	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

const allocUsage = "alloc: name a subcommand: request, ls, show, wait, end, renew or end-run"

// allocCommand is `alloc request|ls|show|wait|end|renew|end-run`. A standing allocation is capacity
// a run holds over time (see the README): jobs submitted into it with `submit --alloc` run inside
// it one at a time and never wait for capacity again. The commands are about the run's allocations,
// so they take no task.
func allocCommand(ctx context.Context, d deps, args []string) int {
	if len(args) == 0 {
		return fail(d, d.jsonErrors, usagef("%s", allocUsage))
	}
	switch args[0] {
	case "request":
		return allocRequest(ctx, d, args[1:])
	case "ls":
		return allocList(ctx, d, args[1:])
	case "show":
		return allocShow(ctx, d, args[1:])
	case "wait":
		return allocWait(ctx, d, args[1:])
	case "end":
		return allocEnd(ctx, d, args[1:])
	case "renew":
		return allocRenew(ctx, d, args[1:])
	case "end-run":
		return allocEndRun(ctx, d, args[1:])
	}
	return fail(d, d.jsonErrors, usagef("alloc: %q is not a subcommand (request, ls, show, wait, end, renew or end-run)", args[0]))
}

// requireRun is the run an allocation command acts for: the calls that change an allocation are a
// run's, and a run is named by --run or VIBESYS_RUN.
func (c *common) requireRun() (string, error) {
	if c.run == "" {
		return "", usagef("--run: name the run that holds the allocation (or set VIBESYS_RUN)")
	}
	return c.run, nil
}

// oneAllocationID reads the one allocation id a command takes.
func oneAllocationID(command string, positional []string) (string, error) {
	if len(positional) != 1 {
		return "", usagef("%s: give one allocation, such as %s a7", command, command)
	}
	return positional[0], nil
}

// allocArgs parses the flags of an alloc subcommand. ok is false when the command is over: help, or
// a flag that did not parse, with the exit code in code.
func allocArgs(d deps, name string, args []string, setup func(c *common, fs *flag.FlagSet)) (positional []string, c *common, code int, ok bool) {
	fs, c := newFlags(name, d)
	if setup != nil {
		setup(c, fs)
	}
	positional, err := parse(fs, args)
	if err != nil {
		return nil, c, parseFailed(d, fs, err), false
	}
	return positional, c, exitOK, true
}

// describeAllocation is the one line an allocation takes: its id first, as a job's is, then its
// state, placement and run, when it started and ends, and the jobs it holds.
func describeAllocation(a *cliv1.Allocation) string {
	fields := []string{a.GetId(), allocationStateName(a.GetState()), a.GetPlacement(), a.GetRun(), "for " + a.GetRequestedFor().AsDuration().String()}
	if a.GetState() == cliv1.AllocationState_ALLOCATION_STATE_PENDING {
		if s := a.GetSlurm(); s != nil && s.GetPendingReason() != "" {
			fields = append(fields, "waiting: "+s.GetPendingReason())
		}
	}
	if a.GetEndsAt() != nil && a.GetState() != cliv1.AllocationState_ALLOCATION_STATE_ENDED {
		fields = append(fields, "ends "+a.GetEndsAt().AsTime().UTC().Format(time.RFC3339))
	}
	if a.GetState() == cliv1.AllocationState_ALLOCATION_STATE_ENDED {
		fields = append(fields, "ended "+a.GetEndedAt().AsTime().UTC().Format(time.RFC3339)+" ("+allocationEndName(a.GetEndReason())+")")
	}
	if a.RunningJob != nil {
		job := "running " + a.GetRunningJob()
		if n := len(a.GetQueuedJobs()); n > 0 {
			job += fmt.Sprintf(" (+%d queued)", n)
		}
		fields = append(fields, job)
	} else if n := len(a.GetQueuedJobs()); n > 0 {
		fields = append(fields, fmt.Sprintf("%d queued", n))
	}
	if r := a.GetRenewal(); r != nil {
		fields = append(fields, fmt.Sprintf("renews %s before its end for %s", r.GetLead().AsDuration(), r.GetSuccessorFor().AsDuration()))
	}
	if a.RenewedBy != nil {
		fields = append(fields, "renewed by "+a.GetRenewedBy())
	}
	if a.RenewalSkippedReason != nil {
		fields = append(fields, "renewal skipped: "+a.GetRenewalSkippedReason())
	}
	return strings.Join(fields, "\t")
}

func allocationStateName(s cliv1.AllocationState) string {
	return strings.ToLower(strings.TrimPrefix(s.String(), "ALLOCATION_STATE_"))
}

func allocationEndName(r cliv1.AllocationEndReason) string {
	return strings.ToLower(strings.TrimPrefix(r.String(), "ALLOCATION_END_REASON_"))
}

func printAllocation(d deps, asJSON bool, msg interface{ GetAllocation() *cliv1.Allocation }, whole func()) {
	if asJSON {
		whole()
		return
	}
	fmt.Fprintln(d.stdout, describeAllocation(msg.GetAllocation()))
}

// allocRequest is `alloc request <placement> --for <duration>`: it returns once the request is
// recorded, usually with the allocation pending.
func allocRequest(ctx context.Context, d deps, args []string) int {
	var forD *time.Duration
	positional, c, code, ok := allocArgs(d, "alloc request", args, func(_ *common, fs *flag.FlagSet) {
		forD = fs.Duration("for", 0, "how long to ask for: the allocation's time limit from its start, such as 4h")
	})
	if !ok {
		return code
	}
	run, err := c.requireRun()
	switch {
	case err != nil:
		return fail(d, c.json, err)
	case len(positional) != 1:
		return fail(d, c.json, usagef("alloc request: give one placement, such as alloc request mi300a --for 4h"))
	case *forD <= 0:
		return fail(d, c.json, usagef("--for: give a positive time, such as 4h"))
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.RequestAllocation(ctx, &cliv1.RequestAllocationRequest{
		RunId: run, Request: &cliv1.AllocationRequest{Placement: positional[0], RequestedFor: durationpb.New(*forD)},
	})
	if err != nil {
		return fail(d, c.json, err)
	}
	printAllocation(d, c.json, resp, func() { printJSON(d, resp) })
	return exitOK
}

// allocList is `alloc ls [--ended] [--all-runs]`: the run's allocations, oldest first.
func allocList(ctx context.Context, d deps, args []string) int {
	var ended, all *bool
	positional, c, code, ok := allocArgs(d, "alloc ls", args, func(_ *common, fs *flag.FlagSet) {
		ended = fs.Bool("ended", false, "also list the allocations that ended")
		all = fs.Bool("all-runs", false, "list the allocations of every run, not only --run's")
	})
	if !ok {
		return code
	}
	run := c.run
	switch {
	case len(positional) != 0:
		return fail(d, c.json, usagef("alloc ls: takes no argument"))
	case *all:
		run = ""
	case run == "":
		return fail(d, c.json, usagef("--run: name the run whose allocations to list, or give --all-runs (or set VIBESYS_RUN)"))
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.ListAllocations(ctx, &cliv1.ListAllocationsRequest{RunId: run, Ended: *ended})
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
		return exitOK
	}
	for _, a := range resp.GetAllocations() {
		fmt.Fprintln(d.stdout, describeAllocation(a))
	}
	return exitOK
}

// allocShow is `alloc show <id>`.
func allocShow(ctx context.Context, d deps, args []string) int {
	positional, c, code, ok := allocArgs(d, "alloc show", args, nil)
	if !ok {
		return code
	}
	id, err := oneAllocationID("alloc show", positional)
	if err != nil {
		return fail(d, c.json, err)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.ShowAllocation(ctx, &cliv1.ShowAllocationRequest{Id: id})
	if err != nil {
		return fail(d, c.json, err)
	}
	printAllocation(d, c.json, resp, func() { printJSON(d, resp) })
	return exitOK
}

// defaultAllocWait is how long `alloc wait` waits when --timeout says nothing: a wait is always
// bounded.
const defaultAllocWait = 30 * time.Minute

// allocWait is `alloc wait <id> --until running|ending-in <duration>|ended [--timeout 30m]`. It exits 0
// when the allocation is in the state, 3 when the timeout passed first, and 1 when the allocation
// ended before it was.
func allocWait(ctx context.Context, d deps, args []string) int {
	var until *string
	var timeout *time.Duration
	positional, c, code, ok := allocArgs(d, "alloc wait", args, func(_ *common, fs *flag.FlagSet) {
		until = fs.String("until", "", "what to wait for: running, ending-in <duration> (the time left is at most that, or it ended), or ended")
		timeout = fs.Duration("timeout", defaultAllocWait, "give up after this long; the allocation is not changed")
	})
	if !ok {
		return code
	}
	req := &cliv1.WaitAllocationRequest{Timeout: durationpb.New(*timeout)}
	var err error
	switch *until {
	case "running":
		req.Until = &cliv1.WaitAllocationRequest_Running{Running: true}
		req.Id, err = oneAllocationID("alloc wait", positional)
	case "ended":
		req.Until = &cliv1.WaitAllocationRequest_Ended{Ended: true}
		req.Id, err = oneAllocationID("alloc wait", positional)
	case "ending-in":
		if len(positional) != 2 {
			return fail(d, c.json, usagef("alloc wait: --until ending-in takes the allocation and a time, such as alloc wait a7 --until ending-in 10m"))
		}
		var left time.Duration
		if left, err = time.ParseDuration(positional[1]); err != nil || left < 0 {
			return fail(d, c.json, usagef("alloc wait: %q is not a time such as 10m", positional[1]))
		}
		req.Id = positional[0]
		req.Until = &cliv1.WaitAllocationRequest_EndingIn{EndingIn: durationpb.New(left)}
	default:
		return fail(d, c.json, usagef("--until: say what to wait for: running, ending-in <duration> or ended"))
	}
	switch {
	case err != nil:
		return fail(d, c.json, err)
	case *timeout <= 0:
		return fail(d, c.json, usagef("--timeout: %v is not positive; a wait is always bounded", *timeout))
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.WaitAllocation(ctx, req)
	if err != nil {
		return fail(d, c.json, err)
	}
	printAllocation(d, c.json, resp, func() { printJSON(d, resp) })
	return exitOfAllocWait(resp.GetOutcome())
}

// exitOfAllocWait is the exit code of an allocation wait. An outcome this build does not know, from
// a newer daemon, is no answer, never success.
func exitOfAllocWait(o cliv1.AllocationWaitOutcome) int {
	switch o {
	case cliv1.AllocationWaitOutcome_ALLOCATION_WAIT_OUTCOME_MET:
		return exitOK
	case cliv1.AllocationWaitOutcome_ALLOCATION_WAIT_OUTCOME_ENDED:
		return exitFailed
	case cliv1.AllocationWaitOutcome_ALLOCATION_WAIT_OUTCOME_TIMED_OUT:
		return exitTimeout
	case cliv1.AllocationWaitOutcome_ALLOCATION_WAIT_OUTCOME_UNSPECIFIED:
		return exitNoAnswer
	}
	// No default above, so archcheck holds the switch to every outcome the contract names.
	return exitNoAnswer
}

// allocEnd is `alloc end <id> [--after-jobs]`.
func allocEnd(ctx context.Context, d deps, args []string) int {
	var after *bool
	positional, c, code, ok := allocArgs(d, "alloc end", args, func(_ *common, fs *flag.FlagSet) {
		after = fs.Bool("after-jobs", false, "end it once the jobs it holds are done, taking no new job meanwhile")
	})
	if !ok {
		return code
	}
	run, err := c.requireRun()
	if err != nil {
		return fail(d, c.json, err)
	}
	id, err := oneAllocationID("alloc end", positional)
	if err != nil {
		return fail(d, c.json, err)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.EndAllocation(ctx, &cliv1.EndAllocationRequest{RunId: run, End: &cliv1.AllocationEnd{Id: id, AfterJobs: *after}})
	if err != nil {
		return fail(d, c.json, err)
	}
	printAllocation(d, c.json, resp, func() { printJSON(d, resp) })
	return exitOK
}

// allocRenew is `alloc renew <id> --lead <d> --for <d> --until <RFC3339>|run-end`, or `--off`: ask for
// a successor once the time left is at most the lead.
func allocRenew(ctx context.Context, d deps, args []string) int {
	var lead, forD *time.Duration
	var until *string
	var off *bool
	positional, c, code, ok := allocArgs(d, "alloc renew", args, func(_ *common, fs *flag.FlagSet) {
		lead = fs.Duration("lead", 0, "ask for the successor once the time left is at most this, such as 30m")
		forD = fs.Duration("for", 0, "what the successor asks for, such as 4h; longer than --lead")
		until = fs.String("until", "", "when the order stops asking for successors: an RFC 3339 time, or run-end")
		off = fs.Bool("off", false, "take the allocation's renewal order away")
	})
	if !ok {
		return code
	}
	run, err := c.requireRun()
	if err != nil {
		return fail(d, c.json, err)
	}
	id, err := oneAllocationID("alloc renew", positional)
	if err != nil {
		return fail(d, c.json, err)
	}
	req := &cliv1.RenewAllocationRequest{RunId: run, Id: id}
	if *off {
		if *lead != 0 || *forD != 0 || *until != "" {
			return fail(d, c.json, usagef("alloc renew: --off takes away the order, so it takes no --lead, --for or --until"))
		}
		req.Order = &cliv1.RenewAllocationRequest_Off{Off: true}
	} else {
		order := &cliv1.RenewalOrder{Lead: durationpb.New(*lead), SuccessorFor: durationpb.New(*forD)}
		switch {
		case *lead <= 0 || *forD <= 0:
			return fail(d, c.json, usagef("alloc renew: give --lead and --for as positive times, such as --lead 30m --for 4h, or --off"))
		case *until == "run-end":
			order.Until = &cliv1.RenewalOrder_RunEnd{RunEnd: true}
		case *until == "":
			return fail(d, c.json, usagef("--until: say when the order stops: an RFC 3339 time, or run-end"))
		default:
			at, err := time.Parse(time.RFC3339, *until)
			if err != nil {
				return fail(d, c.json, usagef("--until: %q is not an RFC 3339 time such as 2026-11-10T18:00:00Z, or run-end", *until))
			}
			order.Until = &cliv1.RenewalOrder_At{At: timestamppb.New(at)}
		}
		req.Order = &cliv1.RenewAllocationRequest_Renewal{Renewal: order}
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.RenewAllocation(ctx, req)
	if err != nil {
		return fail(d, c.json, err)
	}
	printAllocation(d, c.json, resp, func() { printJSON(d, resp) })
	return exitOK
}

// allocEndRun is `alloc end-run`: what a run's end does to its allocations. Whoever ends a run runs
// it, so that nothing the run held outlives it.
func allocEndRun(ctx context.Context, d deps, args []string) int {
	positional, c, code, ok := allocArgs(d, "alloc end-run", args, nil)
	if !ok {
		return code
	}
	run, err := c.requireRun()
	switch {
	case err != nil:
		return fail(d, c.json, err)
	case len(positional) != 0:
		return fail(d, c.json, usagef("alloc end-run: takes no argument"))
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.EndRunAllocations(ctx, &cliv1.EndRunAllocationsRequest{RunId: run})
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
		return exitOK
	}
	for _, a := range resp.GetAllocations() {
		fmt.Fprintln(d.stdout, describeAllocation(a))
	}
	return exitOK
}
