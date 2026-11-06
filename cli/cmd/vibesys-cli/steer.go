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
