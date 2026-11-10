package flow_test

import (
	"context"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// taskOf is the task a plan was made from, as far as the job's limits go.
func taskOf(p flow.Plan) task.Task {
	return task.Task{Build: p.Build, Check: p.Check, Benchmark: p.Benchmark, Samples: p.Samples, Candidate: p.Candidate, Service: p.Service}
}

// What a job is held to is never more than its bound: the limits of every command the machine asks
// for, and of every server it waits for, added up, in a job of every shape. A job of a short task
// therefore fits a short allocation, which a flat default would never let it.
func TestAJobNeverTakesLongerThanItsBound(t *testing.T) {
	for _, s := range shapes() {
		p := s.plan()
		w := &world{p: p, at: -1}
		if _, err := flow.Drive(context.Background(), p, w); err != nil {
			t.Fatal(err)
		}
		var held time.Duration
		for _, r := range w.asked {
			switch r := r.(type) {
			case flow.Run:
				held += r.Timeout
			case flow.Serve:
				held += r.StartupTimeout
			}
		}
		settings := core.Settings(core.CheckSettings{})
		if s.kind == core.JobMeasure {
			settings = core.MeasureSettings{Samples: s.samples}
		}
		bound := flow.Bound(taskOf(p), settings)
		if bound < held {
			t.Errorf("%s: the job is held to %v and its bound is %v", s, held, bound)
		}
		// The bound is no looser than a measure that has its check run: a linked pass skips it.
		if !s.linked && bound != held {
			t.Errorf("%s: the bound %v is not the %v the job is held to", s, bound, held)
		}
	}
}

// More samples never make the bound shorter, and a longer limit never does.
func TestTheBoundGrowsWithTheSamplesAndTheLimits(t *testing.T) {
	base := shape{kind: core.JobMeasure, build: true, protocol2: true, samples: 1, candidate: true}.plan()
	prev := time.Duration(0)
	for n := uint32(0); n < 8; n++ {
		b := flow.Bound(taskOf(base), core.MeasureSettings{Samples: n})
		if n > 1 && b < prev {
			t.Fatalf("%d samples bound %v, fewer bound %v", n, b, prev)
		}
		prev = b
	}
	tk := taskOf(base)
	short := flow.Bound(tk, core.CheckSettings{})
	tk.Check.Timeout += time.Second
	if long := flow.Bound(tk, core.CheckSettings{}); long <= short {
		t.Fatalf("a longer check limit gives bound %v, not over %v", long, short)
	}
}
