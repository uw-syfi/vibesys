package wire_test

import (
	"testing"

	"buf.build/go/protovalidate"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// A result format names exactly one format, and a JSON file names a flag and a field.
func TestAResultFormatNamesExactlyOneFormat(t *testing.T) {
	if err := protovalidate.Validate(&cliv1.ResultFormat{}); err == nil {
		t.Fatal("a result format with no format passes the rules")
	}
	for _, f := range []*cliv1.ResultFormat{
		{Format: &cliv1.ResultFormat_EvaluatorStream{EvaluatorStream: &cliv1.EvaluatorStream{}}},
		{Format: &cliv1.ResultFormat_ScoredField{ScoredField: &cliv1.ScoredField{Flag: "--out", Field: "ops"}}},
	} {
		if err := protovalidate.Validate(f); err != nil {
			t.Errorf("%v: %v", f, err)
		}
	}
	for _, f := range []*cliv1.ResultFormat{
		{Format: &cliv1.ResultFormat_ScoredField{ScoredField: &cliv1.ScoredField{Flag: "out", Field: "ops"}}},
		{Format: &cliv1.ResultFormat_ScoredField{ScoredField: &cliv1.ScoredField{Flag: "--out"}}},
	} {
		if err := protovalidate.Validate(f); err == nil {
			t.Errorf("%v passes the rules", f)
		}
	}
}

// A plan that an earlier build left in a run directory names its format in the earlier fields, and
// reads as the format it names; one that names two, or none, is refused, and so are the rules.
func TestAnEarlierResultFormatIsStillRead(t *testing.T) {
	r := newRand(31)
	for _, c := range []struct {
		f       *cliv1.ResultFormat
		want    task.Result
		invalid bool
	}{
		{f: &cliv1.ResultFormat{Protocol2: true}, want: task.Result{Protocol2: true}},
		{f: &cliv1.ResultFormat{JsonArgument: "--out", Field: "ops"}, want: task.Result{JSONArgument: "--out", Field: "ops"}},
		{f: &cliv1.ResultFormat{}, invalid: true},
		{f: &cliv1.ResultFormat{Field: "ops"}, invalid: true},
		{f: &cliv1.ResultFormat{Protocol2: true, JsonArgument: "--out"}, invalid: true},
		{f: &cliv1.ResultFormat{Protocol2: true, Format: &cliv1.ResultFormat_EvaluatorStream{EvaluatorStream: &cliv1.EvaluatorStream{}}}, invalid: true},
	} {
		if rules := protovalidate.Validate(c.f); (rules != nil) != c.invalid {
			t.Errorf("%v: the rules say %v", c.f, rules)
		}
		p := anyFlowPlan(r)
		for p.Kind != core.JobMeasure {
			p = anyFlowPlan(r)
		}
		msg := wire.Launch(launchWith(r, &p))
		msg.Plan.ResultFormat = c.f
		got, err := wire.ToLaunch(msg)
		if c.invalid {
			if err == nil {
				t.Errorf("%v: a plan with it is read as %+v", c.f, got.Plan.Result)
			}
			continue
		}
		if err != nil || got.Plan.Result != c.want {
			t.Errorf("%v: read as %+v, %v; want %+v", c.f, got.Plan.Result, err, c.want)
		}
	}
}
