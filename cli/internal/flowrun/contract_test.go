package flowrun_test

import (
	"context"
	"io"
	"strings"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/flowrun"
)

// rig is a set of parts a shell can be built on: the fakes, or the real host. The same scenarios
// run on both, so the fake is held to what the real parts do.
type rig interface {
	real() bool
	config(out io.Writer) flowrun.Config
	// leaks says what a finished job left behind: a checkout, a scratch directory.
	leaks() []string
}

// runScenarios runs every scenario on a new rig each, and checks the verdict and that nothing is
// left behind, whatever the ending.
func runScenarios(t *testing.T, newRig func(t *testing.T) rig) {
	for _, s := range scenarios() {
		t.Run(s.name, func(t *testing.T) {
			r := newRig(t)
			out := newLineWatch()
			v, err := flowrun.Run(context.Background(), r.config(out), s.plan(r.real()))
			if err != nil {
				t.Fatalf("Run: %v", err)
			}
			s.verify(t, v)
			if left := r.leaks(); len(left) > 0 {
				t.Errorf("the job left %v behind", left)
			}
		})
	}
}

// runCancellations cancels a job while a command of each stage runs, and checks that the job ends
// as cancelled in that stage and leaves nothing behind. The cancel is sent once the command says
// it runs, so the stage is the one cancelled whatever the speed of the machine.
func runCancellations(t *testing.T, newRig func(t *testing.T) rig) {
	for _, s := range stageHangs() {
		t.Run(s.name, func(t *testing.T) {
			r := newRig(t)
			out := newLineWatch()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			var (
				wg      sync.WaitGroup
				verdict flow.Verdict
				err     error
			)
			wg.Go(func() { verdict, err = flowrun.Run(ctx, r.config(out), s.plan(r.real())) })
			awaitMarker(t, out)
			cancel()
			wg.Wait()
			if err != nil {
				t.Fatalf("Run: %v", err)
			}
			if !verdict.Cancelled() || verdict.Stage != s.stage {
				t.Errorf("verdict = %+v, want cancelled in the %s", verdict, s.stage)
			}
			if verdict.Findings != nil {
				t.Errorf("a cancelled job found %+v", verdict.Findings)
			}
			if left := r.leaks(); len(left) > 0 {
				t.Errorf("the job left %v behind", left)
			}
		})
	}
}

// runOutput checks that the job's output shows what ran, in order, and the commands' own output.
func runOutput(t *testing.T, newRig func(t *testing.T) rig) {
	r := newRig(t)
	var out strings.Builder
	s := scenarios()[2] // a measure job takes its samples
	build := exits(0)
	s.build = &build
	if _, err := flowrun.Run(context.Background(), r.config(&out), s.plan(r.real())); err != nil {
		t.Fatal(err)
	}
	var stages []string
	for _, line := range strings.Split(out.String(), "\n") {
		if strings.HasPrefix(line, "[") {
			stages = append(stages, line[1:strings.Index(line, "]")])
		}
	}
	want := []string{"build", "check", "benchmark", "benchmark", "benchmark"}
	if strings.Join(stages, ",") != strings.Join(want, ",") {
		t.Errorf("the output names the stages %v, want %v:\n%s", stages, want, out.String())
	}
}
