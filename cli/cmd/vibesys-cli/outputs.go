package main

import (
	"context"
	"fmt"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// outputsCommand is `outputs <job> [--to dir]`: the output files a command job submitted with
// --outputs kept, listed with the job's provenance, and with --to copied into a directory. A job
// that kept none is the daemon's refusal, which says why.
func outputsCommand(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("outputs", d)
	to := fs.String("to", "", "copy the files into this directory, made when it is not there, at their paths in the job's artifacts directory; a file already there is replaced")
	ids, err := parse(fs, args)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	if len(ids) != 1 {
		return fail(d, c.json, usagef("outputs: name one job"))
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
	req := &cliv1.ReadOutputsRequest{Context: callCtx, JobId: ids[0]}
	var job *cliv1.Job
	if *to == "" {
		job, err = cl.Outputs(ctx, req)
	} else {
		job, err = cl.CopyOutputs(ctx, req, *to)
	}
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, job)
		return exitOK
	}
	fmt.Fprint(d.stdout, describeOutputs(job, *to))
	return exitOK
}

// describeOutputs is the job, where it ran, and its files with their sizes and digests, and where
// they were copied when they were.
func describeOutputs(job *cliv1.Job, to string) string {
	var b strings.Builder
	fmt.Fprintln(&b, describe(job))
	if env := job.GetEnvironment(); env != nil {
		fmt.Fprintf(&b, "environment: %s\n", environmentName(env))
	}
	files := job.GetOutputs().GetKept().GetOutputs()
	var total uint64
	for _, f := range files {
		total += f.GetSize()
	}
	fmt.Fprintf(&b, "outputs: %s, %s\n", counted(uint64(len(files)), "file"), counted(total, "byte"))
	for _, f := range files {
		fmt.Fprintf(&b, "  %s\t%s\tsha256:%s\n", f.GetName(), counted(f.GetSize(), "byte"), f.GetSha256())
	}
	if to != "" {
		fmt.Fprintf(&b, "copied to %s\n", to)
	}
	return b.String()
}

// counted is n of a noun, singular for one: "1 file", "2 files", "0 bytes".
func counted(n uint64, noun string) string {
	if n == 1 {
		return "1 " + noun
	}
	return fmt.Sprintf("%d %ss", n, noun)
}
