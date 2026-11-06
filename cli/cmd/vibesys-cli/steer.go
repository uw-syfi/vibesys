package main

import (
	"context"
	"fmt"
	"time"

	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
)

// holdCommand is `hold <job> [--reason text]`: the job keeps its place in the queue and is not
// started until `release`.
func holdCommand(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("hold", d)
	reason := fs.String("reason", "", "why the job is held; `jobs` shows it")
	ids, err := parse(fs, args)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	if len(ids) != 1 {
		return fail(d, c.json, usagef("hold: name one job"))
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
	resp, err := cl.HoldJob(ctx, &cliv1.HoldJobRequest{Context: callCtx, JobId: ids[0], Reason: *reason})
	if err != nil {
		return fail(d, c.json, err)
	}
	return printJob(d, c.json, resp, resp.GetJob())
}

// releaseCommand is `release <job>`: a held job starts in its turn again.
func releaseCommand(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("release", d)
	ids, err := parse(fs, args)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	if len(ids) != 1 {
		return fail(d, c.json, usagef("release: name one job"))
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
	resp, err := cl.ReleaseJob(ctx, &cliv1.ReleaseJobRequest{Context: callCtx, JobId: ids[0]})
	if err != nil {
		return fail(d, c.json, err)
	}
	return printJob(d, c.json, resp, resp.GetJob())
}

// jobCommand is `job priority <job> high|normal|low|inherit --reason text`: a queued job's own
// priority, which overrides its plans', or with inherit theirs again.
func jobCommand(ctx context.Context, d deps, args []string) int {
	if len(args) == 0 || args[0] != "priority" {
		return fail(d, d.jsonErrors, usagef("job: name a subcommand: priority"))
	}
	fs, c := newFlags("job priority", d)
	reason := fs.String("reason", "", "why the priority changes, required; kept as a note on the job")
	positional, err := parse(fs, args[1:])
	if err != nil {
		return parseFailed(d, fs, err)
	}
	if len(positional) != 2 {
		return fail(d, c.json, usagef("job priority: give a job and a priority (high, normal, low or inherit)"))
	}
	priority, err := parsePriority("priority", positional[1], true)
	if err != nil {
		return fail(d, c.json, err)
	}
	if *reason == "" {
		return fail(d, c.json, usagef("--reason: a change of priority needs a reason"))
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
	resp, err := cl.SetJobPriority(ctx, &cliv1.SetJobPriorityRequest{Context: callCtx, JobId: positional[0], Priority: priority, Reason: *reason})
	if err != nil {
		return fail(d, c.json, err)
	}
	return printJob(d, c.json, resp, resp.GetJob())
}

// priorityNames maps what a user writes to priorities. It is read from the contract's enum, so a
// priority added there is a priority here.
var priorityNames = enumNames(cliv1.Priority_name, "PRIORITY_", func(n int32) cliv1.Priority { return cliv1.Priority(n) })

func priorityName(p cliv1.Priority) string { return lower("PRIORITY_", p.String()) }

// parsePriority reads a priority by name, and with inherit allowed, "inherit" as none of its own.
func parsePriority(field, name string, inherit bool) (cliv1.Priority, error) {
	if inherit && name == "inherit" {
		return cliv1.Priority_PRIORITY_UNSPECIFIED, nil
	}
	if p, ok := priorityNames[name]; ok {
		return p, nil
	}
	more := ""
	if inherit {
		more = " or inherit"
	}
	return 0, usagef("%s: %q is not a priority (%s%s)", field, name, choices(cliv1.Priority_name, "PRIORITY_"), more)
}

// printJob prints a response that carries one job: the response with --json, and the job's lines
// otherwise.
func printJob(d deps, asJSON bool, resp proto.Message, job *cliv1.Job) int {
	if asJSON {
		printJSON(d, resp)
	} else {
		fmt.Fprintln(d.stdout, describe(job))
	}
	return exitOK
}

// daemonDrain is `daemon drain [--reason text]`: the daemon that runs starts no queued job until
// `daemon resume`, and a restart keeps it draining. It starts no daemon: one that is not running
// starts nothing to drain.
func daemonDrain(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("daemon drain", d)
	reason := fs.String("reason", "", "why the daemon drains; `daemon status` and every queued job's wait show it")
	if _, err := parse(fs, args); err != nil {
		return parseFailed(d, fs, err)
	}
	cl, code, ok := runningDaemon(ctx, d, c.json)
	if !ok {
		return code
	}
	defer cl.Close()
	resp, err := cl.Drain(ctx, &cliv1.DrainRequest{Agent: c.agent, Reason: *reason})
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
	} else {
		fmt.Fprintln(d.stdout, describeDrain(resp.GetDrain()))
	}
	return exitOK
}

// daemonResume is `daemon resume`: the queued jobs start in their turn again.
func daemonResume(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("daemon resume", d)
	if _, err := parse(fs, args); err != nil {
		return parseFailed(d, fs, err)
	}
	cl, code, ok := runningDaemon(ctx, d, c.json)
	if !ok {
		return code
	}
	defer cl.Close()
	resp, err := cl.Resume(ctx)
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
	} else {
		fmt.Fprintln(d.stdout, "resumed")
	}
	return exitOK
}

// runningDaemon reaches the daemon that runs, starting none. With none it prints `not running` (a
// Failure with --json) and the exit code is 4, as for `daemon status`.
func runningDaemon(ctx context.Context, d deps, asJSON bool) (*client.Client, int, bool) {
	cl, err := dialExisting(ctx, d)
	if err == client.ErrNoDaemon {
		if asJSON {
			return nil, fail(d, true, err), false
		}
		fmt.Fprintln(d.stdout, "not running")
		return nil, exitNoAnswer, false
	}
	if err != nil {
		return nil, fail(d, asJSON, err), false
	}
	return cl, exitOK, true
}

// describeDrain is the line a drain takes: since when, who asked and why.
func describeDrain(dr *cliv1.DaemonDrain) string {
	line := "draining\tsince " + dr.GetSince().AsTime().UTC().Format(time.RFC3339)
	if dr.GetAgent() != "" {
		line += "\tby " + dr.GetAgent()
	}
	if dr.GetReason() != "" {
		line += "\t" + dr.GetReason()
	}
	return line
}

// describeHold is the line a held job takes under its own: who held it, when and why.
func describeHold(h *cliv1.JobHold) string {
	line := "held since " + h.GetAt().AsTime().UTC().Format(time.RFC3339)
	if h.GetAgent() != "" {
		line += " by " + h.GetAgent()
	}
	if h.GetReason() != "" {
		line += ": " + h.GetReason()
	}
	return line
}
