package apptainer_test

import (
	"bytes"
	"context"
	"errors"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clusterfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

// Regression: the kill of the processes of a container in a standing allocation that is not
// confirmed is not ignored. Remove returns an error that wraps apptainer.ErrSurvivors and keeps the
// container's record; the engine names the box in TakeSurvivors, once, whether Remove or Close
// reported it, so that the daemon can release the allocation; a kill that later succeeds clears it.
func TestSurvivorsOfAContainerInAStandingAllocationAreReported(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	h, err := newAllocator(t, cluster.Connect()).Request(context.Background(), spec(1, "run-a", 4*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	r := newRig(t, w, cluster, setup{})
	const name = "vibesys-cli-first"
	c := startIn(t, r, name, h.Ref, "")
	if _, err := c.Start([]string{"hold", "forever"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	cluster.SetFaults(func(op string) error {
		if op == "signal" {
			return &core.Failure{Kind: core.FailureUnavailable, Message: "host-a: ssh: connection refused"}
		}
		return nil
	})
	err = r.engine.Remove(context.Background(), name)
	if !errors.Is(err, apptainer.ErrSurvivors) {
		t.Fatalf("Remove = %v, want an error wrapping ErrSurvivors", err)
	}
	c.Close() // reaps again, and fails again
	if got := r.engine.TakeSurvivors(); len(got) != 1 || got[0].Job != h.Ref || got[0].Box != name || !errors.Is(got[0].Err, apptainer.ErrSurvivors) {
		t.Fatalf("survivors %+v, want the box %s of job %s once", got, name, h.Ref)
	}
	if len(cluster.Running()) == 0 {
		t.Fatal("the fault killed the processes")
	}
	// Close alone, with no Remove, reports too.
	c2 := startIn(t, r, "vibesys-cli-second", h.Ref, "")
	if _, err := c2.Start([]string{"hold", "forever"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	c2.Close()
	if got := r.engine.TakeSurvivors(); len(got) != 1 || got[0].Box != "vibesys-cli-second" {
		t.Fatalf("survivors after a Close %+v", got)
	}
	// A box whose kill fails and then succeeds is no survivor.
	c3 := startIn(t, r, "vibesys-cli-third", h.Ref, "")
	if _, err := c3.Start([]string{"hold", "forever"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if err := r.engine.Remove(context.Background(), "vibesys-cli-third"); !errors.Is(err, apptainer.ErrSurvivors) {
		t.Fatalf("Remove = %v", err)
	}
	cluster.SetFaults(nil)
	c3.Close()
	if got := r.engine.TakeSurvivors(); len(got) != 0 {
		t.Errorf("survivors %+v after the kill was confirmed", got)
	}
}
