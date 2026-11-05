package main

import (
	"encoding/json"
	"fmt"
	"math"
	"math/rand/v2"
	"strconv"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protojson"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

var servingTask = core.Objectives{
	Metrics: []core.Metric{
		{Name: "output_tok_s", Direction: core.Maximize},
		{Name: "ttft_p50", Direction: core.Minimize},
	},
	RelativeNoise: 0.01,
}

// measuredOn saves a measurement of a multi-objective commit: five samples of each objective.
func (c *resultsCLI) measuredOn(n int, tok, ttft float64) {
	c.t.Helper()
	five := func(v float64) []float64 { return []float64{v, v, v, v, v} }
	storetest.SeedMeasurement(c.t, c.store, storetest.Measurement{
		Commit: rev(n), Samples: five(tok), At: c.clk.Now(), Objectives: servingTask,
		Others: []core.Observation{{Name: "ttft_p50", Values: five(ttft)}},
	})
}

// The JSON an agent parses: the field names of the contract, a verdict on each objective, and
// the kinds by their contract names.
func TestCompareAndFrontierPrintEveryObjective(t *testing.T) {
	c := newResultsCLI(t)
	c.measuredOn(0, 100, 100)
	c.measuredOn(1, 200, 200)

	r := c.run(withTask("compare", "c1")...)
	mustExit(t, r, exitOK, "compare c1")
	for _, want := range []string{"tradeoff\t" + string(rev(1))[:12], "output_tok_s (maximize)\tbetter", "ttft_p50 (minimize)\tworse"} {
		if !strings.Contains(r.stdout, want) {
			t.Errorf("compare stdout %q lacks %q", r.stdout, want)
		}
	}
	r = c.run(withTask("compare", "c1", "--json")...)
	var cmp struct {
		Comparison struct {
			Verdict    struct{ Kind string }
			Objectives []struct {
				Metric  struct{ Name, Direction string }
				Verdict struct{ Kind string }
			}
		}
	}
	if err := json.Unmarshal([]byte(r.stdout), &cmp); err != nil || cmp.Comparison.Verdict.Kind != "VERDICT_KIND_TRADEOFF" ||
		len(cmp.Comparison.Objectives) != 2 || cmp.Comparison.Objectives[1].Metric.Name != "ttft_p50" ||
		cmp.Comparison.Objectives[1].Verdict.Kind != "VERDICT_KIND_WORSE" {
		t.Fatalf("compare --json = %s (%v)", r.stdout, err)
	}

	r = c.run(withTask("promote", "c1")...)
	mustExit(t, r, exitFailed, "promote c1")
	if !strings.Contains(r.stdout, "tradeoff:") || c.branchAt() != rev(0) {
		t.Fatalf("promote of a tradeoff = %+v", r)
	}

	r = c.run(withTask("frontier", "add", "c1")...)
	mustExit(t, r, exitOK, "frontier add c1")
	if !strings.Contains(r.stdout, "added "+string(rev(1))[:12]+" to the frontier") {
		t.Fatalf("frontier add = %+v", r)
	}
	c.measuredOn(2, 150, 200)
	r = c.run(withTask("frontier", "add", "c2", "--json")...)
	mustExit(t, r, exitFailed, "frontier add c2")
	var refused struct {
		Decision struct {
			Refused struct {
				Reasons []struct{ Kind, Member string }
			}
		}
	}
	if err := json.Unmarshal([]byte(r.stdout), &refused); err != nil || len(refused.Decision.Refused.Reasons) != 1 ||
		refused.Decision.Refused.Reasons[0].Kind != "FRONTIER_REFUSAL_KIND_DOMINATED" || refused.Decision.Refused.Reasons[0].Member != string(rev(1)) {
		t.Fatalf("frontier add --json of a beaten commit = %s (%v)", r.stdout, err)
	}

	r = c.run(withTask("frontier", "list")...)
	mustExit(t, r, exitOK, "frontier list")
	if !strings.Contains(r.stdout, string(rev(1))[:12]) || !strings.Contains(r.stdout, "output_tok_s (maximize) 200 over 5 samples") ||
		!strings.Contains(r.stdout, "ttft_p50 (minimize) 200 over 5 samples") {
		t.Fatalf("frontier list = %+v", r)
	}
	r = c.run(withTask("frontier", "list", "--json")...)
	var listed struct {
		Members []struct {
			Member struct{ Commit string }
			Values []struct {
				Metric  struct{ Name string }
				Value   float64
				Samples int
			}
		}
	}
	if err := json.Unmarshal([]byte(r.stdout), &listed); err != nil || len(listed.Members) != 1 || listed.Members[0].Member.Commit != string(rev(1)) ||
		len(listed.Members[0].Values) != 2 || listed.Members[0].Values[0].Value != 200 {
		t.Fatalf("frontier list --json = %s (%v)", r.stdout, err)
	}

	mustExit(t, c.run("frontier", "--task", "kv"), exitInvalidInput, "frontier")
	mustExit(t, c.run(withTask("frontier", "remove", "c1")...), exitInvalidInput, "frontier remove")
}

// A single-metric task prints compare and promote as it always did: one line of numbers, no
// objectives, and no frontier.
func TestASingleMetricCompareIsPrintedAsBefore(t *testing.T) {
	c := newResultsCLI(t)
	c.measured(0, level)
	c.measured(1, win)
	r := c.run(withTask("compare", "c1")...)
	mustExit(t, r, exitOK, "compare c1")
	lines := strings.Split(strings.TrimSuffix(r.stdout, "\n"), "\n")
	if len(lines) != 3 || !strings.HasPrefix(lines[0], "better\t") || !strings.HasPrefix(lines[1], "  ratio ") || !strings.HasPrefix(lines[2], "  environment ") {
		t.Fatalf("compare = %q", r.stdout)
	}
	r = c.run(withTask("compare", "c1", "--json")...)
	if strings.Contains(r.stdout, "objectives") {
		t.Fatalf("a single-metric comparison carries objectives: %s", r.stdout)
	}
	r = c.run(withTask("promote", "c1", "--json")...)
	mustExit(t, r, exitOK, "promote c1")
	if strings.Contains(r.stdout, "frontier") || strings.Contains(r.stdout, "objectives") {
		t.Fatalf("a single-metric promotion carries a frontier or objectives: %s", r.stdout)
	}
}

// A measurement of a multi-objective task has a headline number on every objective, and show and
// result print each one, in the order the task declares them, rounded from the number compare
// weighs for it; --json carries that number exactly. Show once printed the scored metric alone, and
// the other objective's number was in no output: the raw samples were its only trace. For any
// samples, either objective scored, and a measurement pooled from two jobs, each objective's
// number is the mean compare finds on it.
func TestShowAndResultPrintEveryObjectivesHeadline(t *testing.T) {
	rng := rand.New(rand.NewPCG(13, 17))
	samples := func(level float64) []float64 {
		out := make([]float64, 1+rng.IntN(4))
		for i := range out {
			out[i] = level * (1 + (rng.Float64()-0.5)*0.05)
		}
		return out
	}
	tok, ttft := servingTask.Metrics[0], servingTask.Metrics[1]
	for example := range 20 {
		c := newResultsCLI(t)
		scored := servingTask.Metrics[rng.IntN(2)]
		measure := func(n int, tokLevel, ttftLevel float64) map[string][]float64 {
			values := map[string][]float64{tok.Name: samples(tokLevel), ttft.Name: samples(ttftLevel)}
			other := tok
			if scored == tok {
				other = ttft
			}
			c.clk.Advance(time.Minute)
			storetest.SeedMeasurement(t, c.store, storetest.Measurement{
				Commit: rev(n), Samples: values[scored.Name], At: c.clk.Now(), Objectives: servingTask, Metric: scored,
				Others: []core.Observation{{Name: other.Name, Values: values[other.Name]}},
			})
			return values
		}
		// The base, and a candidate better on both objectives measured in two jobs.
		for range 2 {
			measure(0, 100, 100)
		}
		first, topUp := measure(1, 200, 50), measure(1, 200, 50)

		r := c.run(withTask("compare", "c1", "--json")...)
		mustExit(t, r, exitOK, "compare --json")
		var cmp cliv1.CompareResponse
		if err := protojson.Unmarshal([]byte(r.stdout), &cmp); err != nil || len(cmp.GetComparison().GetObjectives()) != 2 {
			t.Fatalf("example %d: compare --json = %s (%v)", example, r.stdout, err)
		}
		var want []string
		means := map[string]float64{}
		for _, o := range cmp.GetComparison().GetObjectives() {
			v := o.GetVerdict()
			means[o.GetMetric().GetName()] = v.GetCandidateMean()
			want = append(want, fmt.Sprintf("%s (%s)\tmean %s over %d samples", o.GetMetric().GetName(),
				strings.ToLower(strings.TrimPrefix(o.GetMetric().GetDirection().String(), "DIRECTION_")),
				strconv.FormatFloat(v.GetCandidateMean(), 'g', 4, 64), v.GetCandidateSamples()))
		}
		if counts := len(first[ttft.Name]) + len(topUp[ttft.Name]); cmp.GetComparison().GetObjectives()[1].GetVerdict().GetCandidateSamples() != uint32(counts) {
			t.Fatalf("example %d: compare pooled %d ttft samples, want %d", example, cmp.GetComparison().GetObjectives()[1].GetVerdict().GetCandidateSamples(), counts)
		}

		r = c.run(withTask("show", "c1")...)
		mustExit(t, r, exitOK, "show c1")
		lines := strings.Split(strings.TrimSuffix(r.stdout, "\n"), "\n")
		if len(lines) != 1 || !strings.Contains(lines[0], "\tmeasure\t"+strings.Join(want, "\t")+"\tcheck ") {
			t.Fatalf("example %d: show = %q, want one measurement line with %q", example, r.stdout, want)
		}
		r = c.run(withTask("show", "c1", "--json")...)
		var shown cliv1.ShowResponse
		if err := protojson.Unmarshal([]byte(r.stdout), &shown); err != nil || len(shown.GetMeasurements()) != 1 {
			t.Fatalf("example %d: show --json = %s (%v)", example, r.stdout, err)
		}
		m := shown.GetMeasurements()[0].GetMeasure()
		if math.Float64bits(m.GetValue()) != math.Float64bits(means[scored.Name]) || len(m.GetObservations()) != 1 ||
			math.Float64bits(m.GetObservations()[0].GetValue()) != math.Float64bits(means[m.GetObservations()[0].GetName()]) {
			t.Fatalf("example %d: show --json measure = %v, want the means compare weighs exactly, %v", example, m, means)
		}

		// After a promotion, a fresh measurement of the head is the result, on every objective.
		mustExit(t, c.run(withTask("promote", "c1")...), exitOK, "promote c1")
		fresh := measure(1, 200, 50)
		r = c.run(withTask("result")...)
		mustExit(t, r, exitOK, "result")
		for _, o := range servingTask.Metrics {
			line := fmt.Sprintf("%s (%s)\tmean %s over %d samples", o.Name, o.Direction,
				strconv.FormatFloat(core.Headline(fresh[o.Name]), 'g', 4, 64), len(fresh[o.Name]))
			if !strings.Contains(r.stdout, line) {
				t.Fatalf("example %d: result = %q lacks %q", example, r.stdout, line)
			}
		}
	}
}
