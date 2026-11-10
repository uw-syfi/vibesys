package apptainer_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clusterfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

// Regression: a Request, an Observe, a Release and an Adopt run on the cluster's own budget, not
// under the deadline of their caller (the daemon gives each call 30 s, and an sbatch may take up to
// the cluster's two minutes): a caller whose deadline passed has them done anyway, while a caller that
// cancelled has them stopped.
func TestTheAllocatorsCallsDoNotEndWithTheirCallersDeadline(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	a := newAllocator(t, cluster.Connect())
	h, err := a.Request(context.Background(), spec(1, "run-a", time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	expired := func() context.Context {
		timers := clock.NewManualTimers()
		ctx, stop := clock.Deadline(context.Background(), timers, 30*time.Second)
		t.Cleanup(stop)
		<-timers.Armed()
		timers.Fire()
		<-ctx.Done()
		return ctx
	}
	calls := map[string]func(context.Context) error{
		"Request": func(ctx context.Context) error { _, err := a.Request(ctx, spec(2, "run-a", time.Hour)); return err },
		"Observe": func(ctx context.Context) error { _, err := a.Observe(ctx, h); return err },
		"Adopt":   func(ctx context.Context) error { _, err := a.Adopt(ctx); return err },
		"Release": func(ctx context.Context) error { return a.Release(ctx, h) },
	}
	for _, name := range []string{"Request", "Observe", "Adopt", "Release"} {
		if err := calls[name](expired()); err != nil {
			t.Errorf("%s under an expired deadline: %v", name, err)
		}
		cancelled, cancel := context.WithCancel(context.Background())
		cancel()
		if err := calls[name](cancelled); !errors.Is(err, context.Canceled) {
			t.Errorf("%s of a cancelled caller: %v, want the cancel", name, err)
		}
	}
}

// Regression: an sbatch whose reply was lost may have been taken, and its job may show in squeue
// only later. The next Requests of the ID find it by its name, whenever it shows, and submit no
// second job of the name; they fail as unavailable meanwhile, which their caller tries again. Only
// once the cluster's call budget has passed with no sign of the job is another submitted.
func TestARequestWhoseReplyWasLostFindsTheJobWhenItShows(t *testing.T) {
	for unseen := range 4 {
		w := fakeworld.New()
		cluster := clusterfake.New(w.MemoryGates)
		cluster.LoseSubmitReplies(1, unseen)
		a := newAllocator(t, cluster.Connect())
		var h alloc.Handle
		var err error
		for attempt := 0; attempt < unseen+3; attempt++ {
			if h, err = a.Request(context.Background(), spec(1, "run-a", time.Hour)); err == nil {
				break
			}
			if f := core.AsFailure(err); f.Kind != core.FailureUnavailable {
				t.Fatalf("unseen %d, attempt %d: %v, want unavailable", unseen, attempt, err)
			}
		}
		if err != nil || h.Ref == "" {
			t.Fatalf("unseen %d: no allocation found: %+v, %v", unseen, h, err)
		}
		if n := len(cluster.Submitted()); n != 1 {
			t.Fatalf("unseen %d: %d jobs submitted, want 1: %+v", unseen, n, cluster.Submitted())
		}
		if live := cluster.Live(); len(live) != 1 {
			t.Fatalf("unseen %d: live %v, want one job", unseen, live)
		}
	}
}

// A submit that got no answer and whose job never shows is submitted again once the cluster's call
// budget has passed, and not before.
func TestAnUnansweredSubmitIsRepeatedOnlyAfterTheCallBudget(t *testing.T) {
	cluster := clusterfake.New(fakeworld.New().MemoryGates)
	cluster.LoseSubmitReplies(1, 1_000)
	clk := clock.NewManual(clusterfake.Epoch, 0)
	a, err := apptainer.NewAllocator(apptainer.AllocatorConfig{
		Cluster: cluster.Connect(), Owner: "daemon1", Scope: scope, Sizing: standingSizing, Clock: clk,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := a.Request(context.Background(), spec(1, "run-a", time.Hour)); err == nil {
		t.Fatal("a lost reply was an answer")
	}
	clk.Advance(apptainer.CallTimeout - time.Second)
	if _, err := a.Request(context.Background(), spec(1, "run-a", time.Hour)); err == nil || len(cluster.Submitted()) != 1 {
		t.Fatalf("within the call budget: %v, %d submitted; want a refusal and one submit", err, len(cluster.Submitted()))
	}
	clk.Advance(2 * time.Second)
	if _, err := a.Request(context.Background(), spec(1, "run-a", time.Hour)); err != nil || len(cluster.Submitted()) != 2 {
		t.Fatalf("after the call budget: %v, %d submitted; want a second submit", err, len(cluster.Submitted()))
	}
}
