package main

import (
	"context"
	"fmt"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
)

// waitCommand is `wait <job>... [--any] [--timeout d]`. Its exit code says how the jobs ended: 0
// when every job that is final succeeded, 1 when one did not, 3 when the timeout came first.
func waitCommand(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("wait", d)
	any := fs.Bool("any", false, "return when one job is final, not when all are")
	timeout := fs.Duration("timeout", 0, "give up after this long, such as 30m; the jobs are not cancelled")
	ids, err := parse(fs, args)
	if err != nil {
		if isHelp(err) {
			return exitOK
		}
		return exitInvalidInput
	}
	switch {
	case len(ids) == 0:
		return fail(d, c.json, usagef("wait: name at least one job"))
	case *timeout < 0:
		return fail(d, c.json, usagef("--timeout: %v is negative", *timeout))
	}
	callCtx, err := c.callContext(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	return waitFor(ctx, d, cl, callCtx, ids, client.WaitOptions{Any: *any, Timeout: *timeout}, c.json)
}

// waitFor waits and prints the last state of each job: one line each, or one JSON message each.
func waitFor(ctx context.Context, d deps, cl *client.Client, callCtx *cliv1.CallContext, ids []string, opts client.WaitOptions, asJSON bool) int {
	res, err := cl.Wait(ctx, callCtx, ids, opts)
	if err != nil {
		return fail(d, asJSON, err)
	}
	for _, j := range res.Jobs {
		if asJSON {
			printJSON(d, &cliv1.WatchResponse{Job: j})
		} else {
			fmt.Fprintln(d.stdout, describe(j))
		}
	}
	switch {
	case res.TimedOut:
		fmt.Fprint(d.stderr, timeoutNote(opts.Timeout))
		return exitTimeout
	case res.Succeeded:
		return exitOK
	default:
		return exitFailed
	}
}

// cancelCommand is `cancel <job> [--reason text]`. It returns once the job is final, so the
// output says how it ended.
func cancelCommand(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("cancel", d)
	reason := fs.String("reason", "", "why the job is cancelled; waiters see it")
	ids, err := parse(fs, args)
	if err != nil {
		if isHelp(err) {
			return exitOK
		}
		return exitInvalidInput
	}
	if len(ids) != 1 {
		return fail(d, c.json, usagef("cancel: name one job"))
	}
	callCtx, err := c.callContext(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.Cancel(ctx, &cliv1.CancelRequest{Context: callCtx, JobId: ids[0], Reason: *reason})
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
	} else {
		fmt.Fprintln(d.stdout, describe(resp.GetJob()))
	}
	return exitOK
}

// noteCommand is `note <job> <text>`.
func noteCommand(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("note", d)
	positional, err := parse(fs, args)
	if err != nil {
		if isHelp(err) {
			return exitOK
		}
		return exitInvalidInput
	}
	if len(positional) != 2 {
		return fail(d, c.json, usagef("note: give a job and the text of the note, quoted as one argument"))
	}
	callCtx, err := c.callContext(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.AddNote(ctx, &cliv1.AddNoteRequest{Context: callCtx, JobId: positional[0], Text: positional[1]})
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
	} else {
		fmt.Fprintln(d.stdout, describe(resp.GetJob()))
	}
	return exitOK
}

// jobsCommand is `jobs [--commit rev] [--state s] [--kind k] [--note-contains text]`.
func jobsCommand(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("jobs", d)
	var states stateList
	commit := fs.String("commit", "", "only jobs of this commit (any revision)")
	kind := fs.String("kind", "", "only jobs of this kind: check, command or measure")
	contains := fs.String("note-contains", "", "only jobs with a note that contains this text")
	fs.Var(&states, "state", "only jobs in this state; repeat or separate with commas")
	positional, err := parse(fs, args)
	if err != nil {
		if isHelp(err) {
			return exitOK
		}
		return exitInvalidInput
	}
	if len(positional) != 0 {
		return fail(d, c.json, usagef("jobs: unexpected argument %q", positional[0]))
	}
	jobKind, err := jobKindOf(*kind)
	if err != nil {
		return fail(d, c.json, err)
	}
	callCtx, err := c.callContext(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	req := &cliv1.ListJobsRequest{Context: callCtx, States: states, Kind: jobKind, NoteContains: *contains}
	if *commit != "" {
		full, err := d.git.Commit(ctx, callCtx.GetRepository(), *commit)
		if err != nil {
			return fail(d, c.json, err)
		}
		req.Commit = string(full)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.ListJobs(ctx, req)
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
		return exitOK
	}
	for _, j := range resp.GetJobs() {
		fmt.Fprintln(d.stdout, describe(j))
	}
	return exitOK
}

func jobKindOf(name string) (cliv1.JobKind, error) {
	switch strings.ToLower(name) {
	case "":
		return cliv1.JobKind_JOB_KIND_UNSPECIFIED, nil
	case "check":
		return cliv1.JobKind_JOB_KIND_CHECK, nil
	case "command":
		return cliv1.JobKind_JOB_KIND_COMMAND, nil
	case "measure":
		return cliv1.JobKind_JOB_KIND_MEASURE, nil
	default:
		return 0, usagef("--kind: %q is not a kind of job (check, command or measure)", name)
	}
}

// logsCommand is `logs <job> [--follow] [--offset n]`. Without --json it writes the job's output
// as it is, so a caller can pipe it.
func logsCommand(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("logs", d)
	follow := fs.Bool("follow", false, "keep printing as the job writes, until it is final")
	offset := fs.Uint64("offset", 0, "start at this byte of the output")
	ids, err := parse(fs, args)
	if err != nil {
		if isHelp(err) {
			return exitOK
		}
		return exitInvalidInput
	}
	if len(ids) != 1 {
		return fail(d, c.json, usagef("logs: name one job"))
	}
	callCtx, err := c.callContext(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	err = cl.Logs(ctx, &cliv1.ReadLogsRequest{Context: callCtx, JobId: ids[0], Offset: *offset, Follow: *follow}, func(r *cliv1.ReadLogsResponse) error {
		if c.json {
			printJSON(d, r)
			return nil
		}
		_, werr := d.stdout.Write(r.GetData())
		return werr
	})
	if err != nil {
		return fail(d, c.json, err)
	}
	return exitOK
}
