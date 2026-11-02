package main

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
)

// The commands below read results and move the result branch. Each makes one call to the daemon,
// which decides everything; this file turns the answer into text, or into the contract's message
// with --json. Text rounds numbers for a reader. JSON carries the daemon's numbers exactly, and
// so does a verdict: rounding never reaches either.

// withClient runs a call against the daemon, starting it when none runs.
func withClient(ctx context.Context, d deps, asJSON bool, call func(*client.Client) int) int {
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, asJSON, err)
	}
	defer cl.Close()
	return call(cl)
}

// showCommand is `show <commit>`: every result saved for the commit.
func showCommand(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("show", d)
	positional, err := parse(fs, args)
	if err != nil {
		if isHelp(err) {
			return exitOK
		}
		return exitInvalidInput
	}
	if len(positional) != 1 {
		return fail(d, c.json, usagef("show: give one commit"))
	}
	callCtx, err := c.callContext(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	commit, err := d.git.Commit(ctx, callCtx.GetRepository(), positional[0])
	if err != nil {
		return fail(d, c.json, err)
	}
	return withClient(ctx, d, c.json, func(cl *client.Client) int {
		resp, err := cl.Show(ctx, &cliv1.ShowRequest{Context: callCtx, Commit: string(commit)})
		if err != nil {
			return fail(d, c.json, err)
		}
		if c.json {
			printJSON(d, resp)
			return exitOK
		}
		if len(resp.GetResults()) == 0 {
			fmt.Fprintf(d.stdout, "no results for %s\n", shortCommit(string(commit)))
		}
		for _, r := range resp.GetResults() {
			fmt.Fprintln(d.stdout, describeResult(r))
		}
		return exitOK
	})
}

// compareCommand is `compare <commit> [--against <commit>]`. Any verdict is a successful answer;
// a commit with no measurement, or measurements from different environments, is a failure.
func compareCommand(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("compare", d)
	against := fs.String("against", "", "the commit to compare with (default: the head of the result branch)")
	positional, err := parse(fs, args)
	if err != nil {
		if isHelp(err) {
			return exitOK
		}
		return exitInvalidInput
	}
	if len(positional) != 1 {
		return fail(d, c.json, usagef("compare: give one commit"))
	}
	callCtx, err := c.callContext(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	req := &cliv1.CompareRequest{Context: callCtx}
	commit, err := d.git.Commit(ctx, callCtx.GetRepository(), positional[0])
	if err != nil {
		return fail(d, c.json, err)
	}
	req.Commit = string(commit)
	if *against != "" {
		other, err := d.git.Commit(ctx, callCtx.GetRepository(), *against)
		if err != nil {
			return fail(d, c.json, err)
		}
		req.Against = string(other)
	}
	return withClient(ctx, d, c.json, func(cl *client.Client) int {
		resp, err := cl.Compare(ctx, req)
		if err != nil {
			return fail(d, c.json, err)
		}
		if c.json {
			printJSON(d, resp)
			return exitOK
		}
		cmp := resp.GetComparison()
		fmt.Fprintf(d.stdout, "%s\t%s vs %s\n", verdictWord(cmp.GetVerdict()), shortCommit(cmp.GetCommit()), shortCommit(cmp.GetAgainst()))
		fmt.Fprintf(d.stdout, "  %s\n  environment %s\n", describeVerdict(cmp.GetVerdict()), environmentName(cmp.GetEnvironment()))
		return exitOK
	})
}

// promoteCommand is `promote <commit>`. A refusal is a normal answer: it is printed on stdout with
// every reason, and the exit code is 1 because the branch did not move.
func promoteCommand(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("promote", d)
	positional, err := parse(fs, args)
	if err != nil {
		if isHelp(err) {
			return exitOK
		}
		return exitInvalidInput
	}
	if len(positional) != 1 {
		return fail(d, c.json, usagef("promote: give one commit"))
	}
	callCtx, err := c.callContext(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	commit, err := d.git.Commit(ctx, callCtx.GetRepository(), positional[0])
	if err != nil {
		return fail(d, c.json, err)
	}
	return withClient(ctx, d, c.json, func(cl *client.Client) int {
		resp, err := cl.Promote(ctx, &cliv1.PromoteRequest{Context: callCtx, Commit: string(commit)})
		if err != nil {
			return fail(d, c.json, err)
		}
		code := exitOK
		if resp.GetRefused() != nil {
			code = exitFailed
		}
		if c.json {
			printJSON(d, resp)
			return code
		}
		if p := resp.GetPromoted(); p != nil {
			previous := ""
			if p.GetPreviousHead() != "" {
				previous = " (the branch was at " + shortCommit(p.GetPreviousHead()) + ")"
			}
			fmt.Fprintf(d.stdout, "promoted %s%s\n  %s: %s\n", shortCommit(p.GetCommit()), previous, verdictWord(p.GetVerdict()), describeVerdict(p.GetVerdict()))
			return code
		}
		fmt.Fprintf(d.stdout, "refused %s\n", shortCommit(string(commit)))
		for _, reason := range resp.GetRefused().GetReasons() {
			fmt.Fprintf(d.stdout, "  %s: %s\n", refusalName(reason.GetKind()), reason.GetMessage())
			if v := reason.GetVerdict(); v != nil {
				fmt.Fprintf(d.stdout, "    %s\n", describeVerdict(v))
			}
			if reason.GetKind() == cliv1.PromoteRefusalKind_PROMOTE_REFUSAL_KIND_STALE {
				fmt.Fprintf(d.stdout, "    measured against %s, the branch is at %s\n", shortCommit(reason.GetMeasuredAgainst()), shortCommit(reason.GetCurrentHead()))
			}
		}
		return code
	})
}

// resultCommand is `result`: the head and a number measured after it last moved, or why there is
// none. With no number the exit code is 1, so a script cannot take silence for a result.
func resultCommand(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("result", d)
	positional, err := parse(fs, args)
	if err != nil {
		if isHelp(err) {
			return exitOK
		}
		return exitInvalidInput
	}
	if len(positional) != 0 {
		return fail(d, c.json, usagef("result: unexpected argument %q", positional[0]))
	}
	callCtx, err := c.callContext(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	return withClient(ctx, d, c.json, func(cl *client.Client) int {
		resp, err := cl.CurrentResult(ctx, &cliv1.CurrentResultRequest{Context: callCtx})
		if err != nil {
			return fail(d, c.json, err)
		}
		code := exitOK
		if resp.GetCurrent() == nil {
			code = exitFailed
		}
		if c.json {
			printJSON(d, resp)
			return code
		}
		if cur := resp.GetCurrent(); cur != nil {
			fmt.Fprintf(d.stdout, "head %s\n%s\n", shortCommit(cur.GetHead()), describeResult(cur.GetResult()))
			return code
		}
		none := resp.GetNone()
		head := ""
		if none.GetHead() != "" {
			head = " (head " + shortCommit(none.GetHead()) + ")"
		}
		fmt.Fprintf(d.stdout, "no current result: %s%s\n", noResultText(none.GetReason()), head)
		return code
	})
}

// Text. These functions round and name; the JSON path never calls them.

func shortCommit(c string) string {
	if len(c) > 12 {
		return c[:12]
	}
	return c
}

// number rounds to four significant digits.
func number(v float64) string { return strconv.FormatFloat(v, 'g', 4, 64) }

func lower(prefix, name string) string { return strings.ToLower(strings.TrimPrefix(name, prefix)) }

func verdictWord(v *cliv1.Verdict) string { return lower("VERDICT_KIND_", v.GetKind().String()) }

func refusalName(k cliv1.PromoteRefusalKind) string {
	return lower("PROMOTE_REFUSAL_KIND_", k.String())
}

func noResultText(r cliv1.NoCurrentResultReason) string {
	switch r {
	case cliv1.NoCurrentResultReason_NO_CURRENT_RESULT_REASON_NO_HEAD:
		return "nothing was promoted, so there is no head"
	case cliv1.NoCurrentResultReason_NO_CURRENT_RESULT_REASON_NOT_MEASURED_SINCE_PROMOTION:
		return "the head has no measurement and check taken since it last moved; submit a measure with --no-cache"
	}
	return lower("NO_CURRENT_RESULT_REASON_", r.String())
}

// describeVerdict is the numbers of a verdict in a line, rounded. The verdict itself is named by
// the caller.
func describeVerdict(v *cliv1.Verdict) string {
	var b strings.Builder
	fmt.Fprintf(&b, "ratio %s (noise %s), mean %s vs %s, %d vs %d samples",
		number(v.GetRatio()), number(v.GetNoise()), number(v.GetCandidateMean()), number(v.GetBaseMean()),
		v.GetCandidateSamples(), v.GetBaseSamples())
	if v.GetReason() != cliv1.InconclusiveReason_INCONCLUSIVE_REASON_UNSPECIFIED {
		fmt.Fprintf(&b, " [%s]", lower("INCONCLUSIVE_REASON_", v.GetReason().String()))
	}
	return b.String()
}

func environmentName(e *cliv1.EnvironmentFingerprint) string {
	name := lower("ENVIRONMENT_KIND_", e.GetKind().String()) + "/" + lower("BACKEND_KIND_", e.GetBackend().String())
	if e.GetImageDigest() != "" {
		digest := e.GetImageDigest()
		if len(digest) > 19 {
			digest = digest[:19]
		}
		name += " " + digest
	}
	return name
}

// describeResult is one saved result in a line, with its numbers rounded.
func describeResult(r *cliv1.Result) string {
	var b strings.Builder
	fmt.Fprintf(&b, "%s\t", r.GetJobId())
	if m := r.GetMeasure(); m != nil {
		var sum float64
		for _, s := range m.GetSamples() {
			sum += s
		}
		mean := 0.0
		if len(m.GetSamples()) > 0 {
			mean = sum / float64(len(m.GetSamples()))
		}
		fmt.Fprintf(&b, "measure\t%s (%s)\tmean %s over %d samples\tcheck %s", m.GetMetric().GetName(),
			lower("DIRECTION_", m.GetMetric().GetDirection().String()), number(mean), len(m.GetSamples()), m.GetCheckJobId())
	} else {
		b.WriteString("check\tpassed")
	}
	fmt.Fprintf(&b, "\t%s\t%s", environmentName(r.GetEnvironment()), shortCommit(r.GetCommit()))
	return b.String()
}
