package main

import (
	"context"
	"flag"
	"fmt"
	"math"
	"strconv"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// gainReferenceNames maps what a user writes to what a gain bound is a share of. It is read from
// the contract's enum, so a reference added there is a name here.
var gainReferenceNames = enumNames(cliv1.PlanGainReference_name, "PLAN_GAIN_REFERENCE_", func(n int32) cliv1.PlanGainReference { return cliv1.PlanGainReference(n) })

func gainReferenceChoices() string {
	return choices(cliv1.PlanGainReference_name, "PLAN_GAIN_REFERENCE_")
}

func gainReferenceName(r cliv1.PlanGainReference) string {
	return strings.ToLower(strings.TrimPrefix(r.String(), "PLAN_GAIN_REFERENCE_"))
}

// parseShare reads a share written as a fraction, `0.05`, or as a percentage, `5%`. It is finite
// and at least 0.
func parseShare(flagName, text string) (float64, error) {
	t := strings.TrimSpace(text)
	scale := 1.0
	if strings.HasSuffix(t, "%") {
		t, scale = strings.TrimSpace(strings.TrimSuffix(t, "%")), 100
	}
	v, err := strconv.ParseFloat(t, 64)
	if err != nil || math.IsNaN(v) || math.IsInf(v, 0) || v < 0 {
		return 0, usagef("%s: %q is not a share: write a number of at least 0 such as 0.05, or a percentage such as 5%%", flagName, text)
	}
	return v / scale, nil
}

// gainBoundFlags are the flags that state a plan's bound on its possible gain.
type gainBoundFlags struct {
	share optString
	of    optString
}

func addGainBoundFlags(fs *flag.FlagSet, what string) *gainBoundFlags {
	g := &gainBoundFlags{}
	fs.Var(&g.share, "gain-bound", what+" as a share, such as 0.05 or 5%, of what --gain-bound-of names")
	fs.Var(&g.of, "gain-bound-of", "what --gain-bound is a share of ("+gainReferenceChoices()+"; default headline)")
	return g
}

// read returns the bound the flags state, or nil when they state none.
func (g *gainBoundFlags) read() (*cliv1.PlanGainBound, error) {
	if !g.share.set {
		if g.of.set {
			return nil, usagef("--gain-bound-of: give the share with --gain-bound")
		}
		return nil, nil
	}
	share, err := parseShare("--gain-bound", g.share.value)
	if err != nil {
		return nil, err
	}
	ref := cliv1.PlanGainReference_PLAN_GAIN_REFERENCE_HEADLINE
	if g.of.set {
		r, ok := gainReferenceNames[strings.TrimSpace(g.of.value)]
		if !ok {
			return nil, usagef("--gain-bound-of: %q is not something a bound is a share of (%s)", g.of.value, gainReferenceChoices())
		}
		ref = r
	}
	return &cliv1.PlanGainBound{Share: share, RelativeTo: ref}, nil
}

// describeGainBound is a bound as one column of a plan's line.
func describeGainBound(b *cliv1.PlanGainBound) string {
	if b == nil {
		return "no gain bound"
	}
	return fmt.Sprintf("gain <= %s%% of %s", strconv.FormatFloat(b.GetShare()*100, 'g', 6, 64), gainReferenceName(b.GetRelativeTo()))
}

// planCheckGain is `plan check-gain --under share`: whether every plan that is open or parked
// states a bound below the share. The answer is the same whichever way it is printed, and the
// exit code is 0 for either answer, so a caller reads `all_under` and not a code.
func planCheckGain(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("plan check-gain", d)
	under := fs.String("under", "", "the share every open or parked plan's gain bound must be below, such as 0.1 or 10%")
	positional, code, ok := planArgs(fs, args)
	if !ok {
		return code
	}
	if len(positional) != 0 {
		return fail(d, c.json, usagef("plan check-gain: unexpected argument %q", positional[0]))
	}
	if strings.TrimSpace(*under) == "" {
		return fail(d, c.json, usagef("plan check-gain: give the share with --under, such as --under 10%%"))
	}
	share, err := parseShare("--under", *under)
	if err != nil {
		return fail(d, c.json, err)
	}
	callCtx, cl, err := planClient(ctx, d, c)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.ListPlans(ctx, &cliv1.ListPlansRequest{
		Context:  callCtx,
		Statuses: []cliv1.PlanStatus{cliv1.PlanStatus_PLAN_STATUS_OPEN, cliv1.PlanStatus_PLAN_STATUS_PARKED},
	})
	if err != nil {
		return fail(d, c.json, err)
	}
	check, err := wire.CheckGain(resp.GetPlans(), share)
	if err != nil {
		return fail(d, c.json, err)
	}
	if c.json {
		printJSON(d, check)
		return exitOK
	}
	fmt.Fprint(d.stdout, renderGainCheck(check))
	return exitOK
}

// renderGainCheck is the answer as text: the verdict, then one line per plan that keeps it from
// being yes.
func renderGainCheck(c *cliv1.PlanGainCheck) string {
	var b strings.Builder
	share := strconv.FormatFloat(c.GetShare()*100, 'g', 6, 64)
	if c.GetAnswer() == cliv1.PlanGainAnswer_PLAN_GAIN_ANSWER_ALL_UNDER {
		fmt.Fprintf(&b, "yes: all %d open or parked plans state a gain bound below %s%%\n", c.GetCounted(), share)
		return b.String()
	}
	fmt.Fprintf(&b, "no: %d of %d open or parked plans do not state a gain bound below %s%%\n", len(c.GetBlockers()), c.GetCounted(), share)
	for _, p := range c.GetBlockers() {
		fmt.Fprintf(&b, "%d\t%s\t%s\t%s\n", p.GetNumber(), planStatusName(p.GetStatus()), p.GetTitle(), describeGainBound(p.GetGainBound()))
	}
	return b.String()
}
