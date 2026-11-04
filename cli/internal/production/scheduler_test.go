package production

import (
	"errors"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/operator"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fairshare"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fifo"
)

// test-isolation: schedulerOf is the wiring from the operator config to the daemon's scheduler,
// which RunDaemon reaches only with a real runtime directory and index.

// The operator config's scheduler names a policy of the registry and the slots of the placement;
// the config of no file is today's daemon: one slot, fifo.
func TestTheSchedulerIsTheOneTheConfigNames(t *testing.T) {
	c, err := operator.Parse(nil)
	if err != nil {
		t.Fatal(err)
	}
	s, err := schedulerOf(c.Scheduler)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := s.Policy.(fifo.Policy); !ok || len(s.Resources) != 1 || s.Resources[0] != (schedule.Resource{ID: schedule.DefaultPlacement, Capacity: 1}) {
		t.Fatalf("the default scheduler is %+v", s)
	}
	s, err = schedulerOf(operator.Scheduler{Policy: "fair-share", MaxJobs: 4})
	if _, ok := s.Policy.(fairshare.Policy); err != nil || !ok || s.Resources[0].Capacity != 4 {
		t.Fatalf("fair-share with 4 jobs is %+v, %v", s, err)
	}
	_, err = schedulerOf(operator.Scheduler{Policy: "lottery", MaxJobs: 1})
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "scheduler.policy") || !strings.Contains(f.Message, "fair-share, fifo") {
		t.Fatalf("an unknown policy: %v", err)
	}
}
