package main

import (
	"flag"
	"fmt"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// planKindNames maps what a user writes to plan kinds. It is read from the contract's enum, so a
// kind added there is a kind here.
var planKindNames = enumNames(cliv1.PlanKind_name, "PLAN_KIND_", func(n int32) cliv1.PlanKind { return cliv1.PlanKind(n) })

// planKindChoices lists the kinds in the order the contract numbers them.
func planKindChoices() string { return choices(cliv1.PlanKind_name, "PLAN_KIND_") }

func planKindName(k cliv1.PlanKind) string {
	return strings.ToLower(strings.TrimPrefix(k.String(), "PLAN_KIND_"))
}

func parsePlanKind(flagName, name string) (cliv1.PlanKind, error) {
	k, ok := planKindNames[strings.TrimSpace(name)]
	if !ok {
		return 0, usagef("%s: %q is not a plan kind (%s)", flagName, name, planKindChoices())
	}
	return k, nil
}

// planKindFlag is the one kind --kind gives on plan create and plan edit.
type planKindFlag struct{ value optString }

func addPlanKindFlag(fs *flag.FlagSet, what string) *planKindFlag {
	k := &planKindFlag{}
	fs.Var(&k.value, "kind", what+" ("+planKindChoices()+")")
	return k
}

// read returns the kind the flag gave, and false when it was not given.
func (k *planKindFlag) read() (cliv1.PlanKind, bool, error) {
	if !k.value.set {
		return 0, false, nil
	}
	kind, err := parsePlanKind("--kind", k.value.value)
	return kind, err == nil, err
}

// planKindList is plan list's --kind: repeatable, and also a comma-separated list.
type planKindList []cliv1.PlanKind

func (l *planKindList) String() string { return fmt.Sprint([]cliv1.PlanKind(*l)) }

func (l *planKindList) Set(v string) error {
	for _, name := range strings.Split(v, ",") {
		k, err := parsePlanKind("--kind", name)
		if err != nil {
			return err
		}
		*l = append(*l, k)
	}
	return nil
}

// noDependencies is what --depends-on takes to say a plan depends on no plan, which plan edit uses
// to clear a plan's dependencies.
const noDependencies = "none"

// dependsOnFlag is --depends-on: plan numbers, as `3` or `#3`, separated by commas or given by
// repeating the flag, or none. The flag is the whole set: on plan edit it replaces the plan's
// dependencies.
type dependsOnFlag struct {
	plans []uint32
	none  bool
	set   bool
}

func addDependsOnFlag(fs *flag.FlagSet, what string) *dependsOnFlag {
	d := &dependsOnFlag{}
	fs.Var(d, "depends-on", what+`: plan numbers separated by commas, or "`+noDependencies+`"`)
	return d
}

func (d *dependsOnFlag) String() string { return fmt.Sprint(d.plans) }

func (d *dependsOnFlag) Set(v string) error {
	d.set = true
	if strings.TrimSpace(v) == noDependencies {
		d.none = true
		return nil
	}
	for _, arg := range strings.Split(v, ",") {
		n, err := parsePlanNumber(strings.TrimSpace(arg))
		if err != nil {
			return err
		}
		d.plans = append(d.plans, n)
	}
	return nil
}

// read returns the plan numbers given, and false when the flag was not. "none" together with a
// number is a mistake.
func (d *dependsOnFlag) read() ([]uint32, bool, error) {
	if d.none && len(d.plans) > 0 {
		return nil, false, usagef("--depends-on: give plan numbers or %q, not both", noDependencies)
	}
	return d.plans, d.set, nil
}

// describeDependsOn is a plan's dependencies on its one line: "depends on 1,3", or "" for none.
func describeDependsOn(ns []uint32) string {
	if len(ns) == 0 {
		return ""
	}
	parts := make([]string, 0, len(ns))
	for _, n := range ns {
		parts = append(parts, fmt.Sprint(n))
	}
	return "depends on " + strings.Join(parts, ",")
}

// renderPlanLinks writes the plans a plan depends on and the plans that depend on it, one line
// each, with where each stands.
func renderPlanLinks(b *strings.Builder, v *cliv1.PlanView) {
	if ds := v.GetDependsOn(); len(ds) > 0 {
		fmt.Fprintf(b, "depends on:\n")
		for _, p := range ds {
			fmt.Fprintf(b, "  %s\n", describePlanSummary(p))
		}
	}
	if ds := v.GetDependents(); len(ds) > 0 {
		fmt.Fprintf(b, "dependents:\n")
		for _, p := range ds {
			fmt.Fprintf(b, "  %s\n", describePlanSummary(p))
		}
	}
}

func describePlanSummary(p *cliv1.PlanSummary) string {
	line := fmt.Sprintf("%d\t%s\t%s", p.GetNumber(), planStatusName(p.GetStatus()), p.GetTitle())
	if p.GetKind() != cliv1.PlanKind_PLAN_KIND_UNSPECIFIED {
		line += "\t" + planKindName(p.GetKind())
	}
	return line
}
