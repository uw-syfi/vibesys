package main

import (
	"context"
	"fmt"
	"math"
	"strconv"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

const boundUsage = "bound: name a subcommand: declare or list"

// boundCommand is `bound declare|list`. A bound is the performance bound of an objective of the
// task, the best it can reach, as an agent derives it from first principles: its speed of light.
// The daemon records every declaration with who made it and when, never replaces one, and compares
// nothing with it; the agent decides when to declare and when to revise.
func boundCommand(ctx context.Context, d deps, args []string) int {
	if len(args) == 0 {
		return fail(d, d.jsonErrors, usagef("%s", boundUsage))
	}
	switch args[0] {
	case "declare":
		return boundDeclare(ctx, d, args[1:])
	case "list":
		return boundList(ctx, d, args[1:])
	}
	return fail(d, d.jsonErrors, usagef("bound: %q is not a subcommand (declare or list)", args[0]))
}

// parseBoundValue reads the number of a bound: finite and above zero.
func parseBoundValue(text string) (float64, error) {
	v, err := strconv.ParseFloat(strings.TrimSpace(text), 64)
	if err != nil || math.IsNaN(v) || math.IsInf(v, 0) || v <= 0 {
		return 0, usagef("--value: %q is not a bound: give a number above 0, such as 15600", text)
	}
	return v, nil
}

// boundDeclare is `bound declare --objective name --value v --unit u --note text [--commit rev]`.
// The daemon reads the task at the commit (the checkout's HEAD by default) to check that the
// objective is one the task declares, and fills in which way it improves.
func boundDeclare(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("bound declare", d)
	objective := fs.String("objective", "", "the objective the bound is for, one the task declares")
	value := fs.String("value", "", "the bound: the best the objective can reach, in the unit of its measurements")
	unit := fs.String("unit", "", `what the value is counted in, such as "tok/s" or "ms"`)
	note := addTextFlag(fs, "note", "how the bound was derived")
	revision := fs.String("commit", "HEAD", "the commit whose task the objective is checked against")
	positional, code, ok := planArgs(fs, args)
	if !ok {
		return code
	}
	if len(positional) != 0 {
		return fail(d, c.json, usagef("bound declare: unexpected argument %q; name the objective with --objective", positional[0]))
	}
	switch {
	case strings.TrimSpace(*objective) == "":
		return fail(d, c.json, usagef("--objective: name the objective the bound is for"))
	case strings.TrimSpace(*value) == "":
		return fail(d, c.json, usagef("--value: give the bound"))
	case strings.TrimSpace(*unit) == "":
		return fail(d, c.json, usagef("--unit: say what the value is counted in"))
	}
	v, err := parseBoundValue(*value)
	if err != nil {
		return fail(d, c.json, err)
	}
	text, given, err := note.read(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	if !given || strings.TrimSpace(text) == "" {
		return fail(d, c.json, usagef("bound declare: say how the bound was derived with --note or --note-file"))
	}
	callCtx, cl, err := planClient(ctx, d, c)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	commit, err := d.commit(ctx, callCtx, *revision)
	if err != nil {
		return fail(d, c.json, err)
	}
	resp, err := cl.DeclareBound(ctx, &cliv1.DeclareBoundRequest{
		Context: callCtx, Commit: commit, Objective: *objective, Value: v, Unit: *unit, Note: text,
	})
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, resp)
		return exitOK
	}
	fmt.Fprintln(d.stdout, describeBound(resp.GetBound()))
	return exitOK
}

// boundList is `bound list [--objective name]`: every declared bound, oldest first, each with the
// bound that stands for its objective marked, and the note that says how it was derived.
func boundList(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("bound list", d)
	objective := fs.String("objective", "", "only the bounds of this objective")
	positional, code, ok := planArgs(fs, args)
	if !ok {
		return code
	}
	if len(positional) != 0 {
		return fail(d, c.json, usagef("bound list: unexpected argument %q", positional[0]))
	}
	callCtx, cl, err := planClient(ctx, d, c)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.ListBounds(ctx, &cliv1.ListBoundsRequest{Context: callCtx})
	if err != nil {
		return fail(d, c.json, err)
	}
	standing, err := wire.StandingBounds(resp.GetBounds())
	if err != nil {
		return fail(d, c.json, err)
	}
	stands := map[uint64]bool{}
	for _, b := range standing {
		stands[b.GetSeq()] = true
	}
	var shown []*cliv1.ObjectiveBound
	for _, b := range resp.GetBounds() {
		if *objective == "" || b.GetObjective() == *objective {
			shown = append(shown, b)
		}
	}
	if c.json {
		printJSON(d, &cliv1.ListBoundsResponse{Bounds: shown})
		return exitOK
	}
	for _, b := range shown {
		state := "superseded"
		if stands[b.GetSeq()] {
			state = "current"
		}
		fmt.Fprintf(d.stdout, "%s\t%s\t%s\t%s\n", describeBound(b), clockText(b.GetOrigin().GetAt().AsTime()), originName(b.GetOrigin()), state)
		for _, line := range strings.Split(strings.TrimRight(b.GetNote(), "\n"), "\n") {
			fmt.Fprintf(d.stdout, "    %s\n", line)
		}
	}
	return exitOK
}

// describeBound is the one line a bound takes: its place in the log first, as a plan's number is, so
// a caller can read it back, then the objective, the number with its unit, and which way the
// objective improves.
func describeBound(b *cliv1.ObjectiveBound) string {
	direction := strings.ToLower(strings.TrimPrefix(b.GetDirection().String(), "DIRECTION_"))
	return fmt.Sprintf("%d\t%s\t%s %s\t%s", b.GetSeq(), b.GetObjective(), strconv.FormatFloat(b.GetValue(), 'g', -1, 64), b.GetUnit(), direction)
}

// originName is who declared a record: the agent, and the run when it is in one.
func originName(o *cliv1.RecordOrigin) string {
	switch {
	case o.GetAgent() == "" && o.GetRunId() == "":
		return "no run"
	case o.GetRunId() == "":
		return o.GetAgent()
	case o.GetAgent() == "":
		return o.GetRunId()
	}
	return o.GetAgent() + " (" + o.GetRunId() + ")"
}
