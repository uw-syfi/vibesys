package main

import (
	"context"
	"fmt"
	"slices"
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
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	commit, err := d.commit(ctx, callCtx, positional[0])
	if err != nil {
		return fail(d, c.json, err)
	}
	return withClient(ctx, d, c.json, func(cl *client.Client) int {
		resp, err := cl.Show(ctx, &cliv1.ShowRequest{Context: callCtx, Commit: commit})
		if err != nil {
			return fail(d, c.json, err)
		}
		if c.json {
			printJSON(d, resp)
			return exitOK
		}
		if len(resp.GetResults()) == 0 {
			fmt.Fprintf(d.stdout, "no results for %s\n", shortCommit(commit))
		}
		// A commit's measurements are printed pooled, one line per kind, as compare counts them:
		// a measure job that topped up an earlier one is part of the same measurement.
		for _, r := range resp.GetResults() {
			if r.GetCheck() != nil {
				fmt.Fprintln(d.stdout, describeResult(r))
			}
		}
		placed := map[string]string{}
		for _, r := range resp.GetResults() {
			placed[r.GetJobId()] = placementOf(r.GetEnvironment())
		}
		for _, m := range resp.GetMeasurements() {
			fmt.Fprintln(d.stdout, describeMeasurement(m, placed))
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
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	req := &cliv1.CompareRequest{Context: callCtx}
	commit, err := d.commit(ctx, callCtx, positional[0])
	if err != nil {
		return fail(d, c.json, err)
	}
	req.Commit = commit
	if *against != "" {
		other, err := d.commit(ctx, callCtx, *against)
		if err != nil {
			return fail(d, c.json, err)
		}
		req.Against = other
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
		for _, line := range verdictLines(cmp.GetVerdict(), cmp.GetObjectives()) {
			fmt.Fprintf(d.stdout, "  %s\n", line)
		}
		fmt.Fprintf(d.stdout, "  environment %s\n", environmentName(cmp.GetEnvironment()))
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
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	commit, err := d.commit(ctx, callCtx, positional[0])
	if err != nil {
		return fail(d, c.json, err)
	}
	return withClient(ctx, d, c.json, func(cl *client.Client) int {
		resp, err := cl.Promote(ctx, &cliv1.PromoteRequest{Context: callCtx, Commit: commit})
		if err != nil {
			return fail(d, c.json, err)
		}
		code := exitOfAnswer(answerOf(resp.GetPromoted() != nil, resp.GetRefused() != nil))
		if c.json {
			printJSON(d, resp)
			return code
		}
		if code == exitNoAnswer {
			fmt.Fprintln(d.stderr, unreadAnswer)
			return code
		}
		if p := resp.GetPromoted(); p != nil {
			previous := ""
			if p.GetPreviousHead() != "" {
				previous = " (the branch was at " + shortCommit(p.GetPreviousHead()) + ")"
			}
			if len(p.GetObjectives()) == 0 {
				fmt.Fprintf(d.stdout, "promoted %s%s\n  %s: %s\n", shortCommit(p.GetCommit()), previous, verdictWord(p.GetVerdict()), describeVerdict(p.GetVerdict()))
				return code
			}
			fmt.Fprintf(d.stdout, "promoted %s%s\n  %s\n", shortCommit(p.GetCommit()), previous, verdictWord(p.GetVerdict()))
			for _, line := range describeObjectiveVerdicts(p.GetObjectives()) {
				fmt.Fprintf(d.stdout, "    %s\n", line)
			}
			if f := resp.GetFrontier(); f != nil {
				for _, line := range strings.Split(strings.TrimSuffix(describeFrontierDecision(f, p.GetCommit()), "\n"), "\n") {
					fmt.Fprintf(d.stdout, "  %s\n", line)
				}
			}
			return code
		}
		fmt.Fprintf(d.stdout, "refused %s\n", shortCommit(commit))
		for _, reason := range resp.GetRefused().GetReasons() {
			fmt.Fprintf(d.stdout, "  %s: %s\n", refusalName(reason.GetKind()), reason.GetMessage())
			if v := reason.GetVerdict(); v != nil {
				for _, line := range verdictLines(v, reason.GetObjectives()) {
					fmt.Fprintf(d.stdout, "    %s\n", line)
				}
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
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	return withClient(ctx, d, c.json, func(cl *client.Client) int {
		resp, err := cl.CurrentResult(ctx, &cliv1.CurrentResultRequest{Context: callCtx})
		if err != nil {
			return fail(d, c.json, err)
		}
		code := exitOfAnswer(answerOf(resp.GetCurrent() != nil, resp.GetNone() != nil))
		if c.json {
			printJSON(d, resp)
			return code
		}
		if code == exitNoAnswer {
			fmt.Fprintln(d.stderr, unreadAnswer)
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

// unreadAnswer is what a command says of an answer from a newer daemon that this build cannot read.
const unreadAnswer = "vibesys-cli: the daemon's answer is one this build cannot read; upgrade vibesys-cli"

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
// the caller. A verdict that compared nothing, because the two sides were measured under another
// task version or for another metric, has no numbers: printing its zeros would read as a measured
// zero, so the line says the sides are not comparable and why.
func describeVerdict(v *cliv1.Verdict) string {
	var b strings.Builder
	if why, ok := notComparable[v.GetReason()]; ok {
		fmt.Fprintf(&b, "not comparable: %s; %d vs %d samples", why, v.GetCandidateSamples(), v.GetBaseSamples())
	} else {
		fmt.Fprintf(&b, "ratio %s (noise %s), mean %s vs %s, %d vs %d samples",
			number(v.GetRatio()), number(v.GetNoise()), number(v.GetCandidateMean()), number(v.GetBaseMean()),
			v.GetCandidateSamples(), v.GetBaseSamples())
	}
	if v.GetReason() != cliv1.InconclusiveReason_INCONCLUSIVE_REASON_UNSPECIFIED {
		fmt.Fprintf(&b, " [%s]", lower("INCONCLUSIVE_REASON_", v.GetReason().String()))
	}
	return b.String()
}

// notComparable are the inconclusive reasons whose verdict holds no numbers, with what to say.
var notComparable = map[cliv1.InconclusiveReason]string{
	cliv1.InconclusiveReason_INCONCLUSIVE_REASON_TASK_VERSION_MISMATCH: "the two commits were measured under different task versions",
	cliv1.InconclusiveReason_INCONCLUSIVE_REASON_METRIC_MISMATCH:       "the two commits were measured for different metrics",
}

// verdictLines is the numbers behind a verdict: its own for a single metric, and a line per
// objective for a multi-objective comparison, whose overall verdict has no numbers of its own.
func verdictLines(v *cliv1.Verdict, objectives []*cliv1.ObjectiveVerdict) []string {
	if len(objectives) == 0 {
		return []string{describeVerdict(v)}
	}
	return describeObjectiveVerdicts(objectives)
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

// factPlacement is the fact of a docker environment that names the placement its container ran on
// (EnvironmentFingerprint.facts).
const factPlacement = "placement"

// placementOf is the placement a docker environment ran on, and empty for any other.
func placementOf(e *cliv1.EnvironmentFingerprint) string {
	if e.GetKind() != cliv1.EnvironmentKind_ENVIRONMENT_KIND_DOCKER {
		return ""
	}
	return e.GetFacts()[factPlacement]
}

// describeMeasurement is a commit's pooled measurement in a line, with its numbers rounded: the
// jobs it pools, then what describeResult says of a measurement, and the placements its jobs ran
// on, each once in the jobs' order, from placed (by job id).
func describeMeasurement(m *cliv1.Measurement, placed map[string]string) string {
	env := m.GetEnvironment()
	line := describeResult(&cliv1.Result{
		JobId: strings.Join(m.GetJobIds(), ","), Commit: m.GetCommit(),
		Environment: &cliv1.EnvironmentFingerprint{Kind: env.GetKind(), Backend: env.GetBackend(), ImageDigest: env.GetImageDigest()},
		Outcome:     &cliv1.Result_Measure{Measure: m.GetMeasure()},
	})
	var on []string
	for _, id := range m.GetJobIds() {
		if p := placed[id]; p != "" && !slices.Contains(on, p) {
			on = append(on, p)
		}
	}
	if len(on) > 0 {
		line += "\ton " + strings.Join(on, ", ")
	}
	return line
}

// describeResult is one saved result in a line, with its numbers rounded, and the placement it was
// taken on, for telling where a number came from.
func describeResult(r *cliv1.Result) string {
	var b strings.Builder
	fmt.Fprintf(&b, "%s\t", r.GetJobId())
	if m := r.GetMeasure(); m != nil {
		fmt.Fprintf(&b, "measure\t%s (%s)\tmean %s over %d samples\tcheck %s", m.GetMetric().GetName(),
			lower("DIRECTION_", m.GetMetric().GetDirection().String()), number(m.GetValue()), len(m.GetSamples()), m.GetCheckJobId())
	} else {
		b.WriteString("check\tpassed")
	}
	fmt.Fprintf(&b, "\t%s\t%s", environmentName(r.GetEnvironment()), shortCommit(r.GetCommit()))
	if p := placementOf(r.GetEnvironment()); p != "" {
		fmt.Fprintf(&b, "\ton %s", p)
	}
	return b.String()
}
