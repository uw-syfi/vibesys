package main

import (
	"context"
	"fmt"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
)

const frontierUsage = "frontier: name a subcommand: add or list"

// frontierCommand is `frontier add|list`: the commits of a multi-objective task that no member
// beats. The daemon decides membership and computes the numbers; this file prints them.
func frontierCommand(ctx context.Context, d deps, args []string) int {
	if len(args) == 0 {
		return fail(d, d.jsonErrors, usagef("%s", frontierUsage))
	}
	sub, ok := frontierSubcommands[args[0]]
	if !ok {
		return fail(d, d.jsonErrors, usagef("frontier: %q is not a subcommand (add or list)", args[0]))
	}
	return sub(ctx, d, args[1:])
}

var frontierSubcommands = map[string]func(context.Context, deps, []string) int{
	"add":  frontierAdd,
	"list": frontierList,
}

// frontierAdd is `frontier add <commit>`. A refusal is a normal answer: it is printed on stdout
// with every reason, and the exit code is 1 because the frontier did not take the commit.
func frontierAdd(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("frontier add", d)
	positional, err := parse(fs, args)
	if err != nil {
		if isHelp(err) {
			return exitOK
		}
		return exitInvalidInput
	}
	if len(positional) != 1 {
		return fail(d, c.json, usagef("frontier add: give one commit"))
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
		resp, err := cl.AddToFrontier(ctx, &cliv1.AddToFrontierRequest{Context: callCtx, Commit: commit})
		if err != nil {
			return fail(d, c.json, err)
		}
		decision := resp.GetDecision()
		code := exitOfAnswer(answerOf(decision.GetAdmitted() != nil, decision.GetRefused() != nil))
		if c.json {
			printJSON(d, resp)
			return code
		}
		if code == exitNoAnswer {
			fmt.Fprintln(d.stderr, unreadAnswer)
			return code
		}
		fmt.Fprint(d.stdout, describeFrontierDecision(decision, commit))
		return code
	})
}

// frontierList is `frontier list`: every member, in the order admitted, with its headline number
// on each objective.
func frontierList(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("frontier list", d)
	positional, err := parse(fs, args)
	if err != nil {
		if isHelp(err) {
			return exitOK
		}
		return exitInvalidInput
	}
	if len(positional) != 0 {
		return fail(d, c.json, usagef("frontier list: unexpected argument %q", positional[0]))
	}
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	return withClient(ctx, d, c.json, func(cl *client.Client) int {
		resp, err := cl.ListFrontier(ctx, &cliv1.ListFrontierRequest{Context: callCtx})
		if err != nil {
			return fail(d, c.json, err)
		}
		if c.json {
			printJSON(d, resp)
			return exitOK
		}
		if len(resp.GetMembers()) == 0 {
			fmt.Fprintln(d.stdout, "the frontier is empty")
		}
		for _, e := range resp.GetMembers() {
			fmt.Fprintf(d.stdout, "%s\t%s\n", shortCommit(e.GetMember().GetCommit()), describeValues(e.GetValues()))
		}
		return exitOK
	})
}

// describeFrontierDecision is what the frontier made of a commit, in lines.
func describeFrontierDecision(decision *cliv1.FrontierDecision, commit string) string {
	var b strings.Builder
	if a := decision.GetAdmitted(); a != nil {
		switch {
		case a.GetAlreadyMember():
			fmt.Fprintf(&b, "%s is on the frontier already\n", shortCommit(a.GetAdded().GetCommit()))
		default:
			fmt.Fprintf(&b, "added %s to the frontier", shortCommit(a.GetAdded().GetCommit()))
			if evicted := a.GetEvicted(); len(evicted) > 0 {
				short := make([]string, len(evicted))
				for i, e := range evicted {
					short[i] = shortCommit(e)
				}
				fmt.Fprintf(&b, ", evicting %s, which it beats", strings.Join(short, ", "))
			}
			if retired := a.GetRetired(); len(retired) > 0 {
				short := make([]string, len(retired))
				for i, r := range retired {
					short[i] = shortCommit(r)
				}
				fmt.Fprintf(&b, ", retiring %s, measured only under another task version", strings.Join(short, ", "))
			}
			b.WriteString("\n")
		}
		fmt.Fprintf(&b, "  members now: %d\n", len(a.GetMembers()))
		return b.String()
	}
	fmt.Fprintf(&b, "not added %s to the frontier\n", shortCommit(commit))
	for _, reason := range decision.GetRefused().GetReasons() {
		fmt.Fprintf(&b, "  %s: %s\n", lower("FRONTIER_REFUSAL_KIND_", reason.GetKind().String()), reason.GetMessage())
		for _, line := range describeObjectiveVerdicts(reason.GetObjectives()) {
			fmt.Fprintf(&b, "    %s\n", line)
		}
	}
	return b.String()
}

// describeValues is a member's headline number on each objective, rounded.
func describeValues(values []*cliv1.ObjectiveValue) string {
	parts := make([]string, len(values))
	for i, v := range values {
		parts[i] = fmt.Sprintf("%s (%s) %s over %d samples", v.GetMetric().GetName(),
			lower("DIRECTION_", v.GetMetric().GetDirection().String()), number(v.GetValue()), v.GetSamples())
	}
	return strings.Join(parts, "\t")
}

// describeObjectiveVerdicts is a line per objective: its name, direction, verdict and numbers.
func describeObjectiveVerdicts(verdicts []*cliv1.ObjectiveVerdict) []string {
	lines := make([]string, len(verdicts))
	for i, v := range verdicts {
		lines[i] = fmt.Sprintf("%s (%s)\t%s\t%s", v.GetMetric().GetName(), lower("DIRECTION_", v.GetMetric().GetDirection().String()),
			verdictWord(v.GetVerdict()), describeVerdict(v.GetVerdict()))
	}
	return lines
}
