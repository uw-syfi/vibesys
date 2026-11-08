package app_test

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"testing"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
)

type placementProbe struct {
	fail bool
	wait bool
}

func (p placementProbe) Offers(ctx context.Context) ([]*cliv1.AcceleratorOffer, error) {
	if p.wait {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	if p.fail {
		return nil, errors.New("unreadable")
	}
	return []*cliv1.AcceleratorOffer{{Vendor: cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, Model: cliv1.AcceleratorModel_ACCELERATOR_MODEL_H100, Count: 1}}, nil
}

func TestPlacementReaderReportsEveryConfiguredPlacementAndOnlySuccessfulOffers(t *testing.T) {
	rng := rand.New(rand.NewPCG(1, 2))
	for range 200 {
		cfg := map[string]app.Placement{}
		for i := 0; i < rng.IntN(20); i++ {
			cfg[fmt.Sprintf("place-%d", i)] = app.Placement{Kind: cliv1.PlacementKind(1 + rng.IntN(4)), MaxJobs: 1, Probe: placementProbe{fail: rng.IntN(2) == 0}}
		}
		reader := app.NewPlacementReader(cfg, clock.NewManual(time.Unix(1, 0), 0), clock.NewManualTimers(), time.Minute)
		got, err := reader.List(t.Context(), true)
		if err != nil {
			t.Fatal(err)
		}
		if len(got) != len(cfg) {
			t.Fatalf("got %d placements, want %d", len(got), len(cfg))
		}
		seen := map[string]bool{}
		for _, v := range got {
			p, ok := cfg[v.GetName()]
			if !ok || seen[v.GetName()] || v.GetKind() != p.Kind {
				t.Fatalf("unexpected placement %v", v)
			}
			seen[v.GetName()] = true
			success := !p.Probe.(placementProbe).fail
			if (v.GetKnown() != nil) != success || (v.GetUnknown() != nil) == success || (v.GetKnown() != nil && len(v.GetKnown().GetOffers()) != 1) {
				t.Fatalf("%s: %v", v.GetName(), v)
			}
		}
	}
}

func TestPlacementProbeTimeoutUsesInjectedTimer(t *testing.T) {
	reader := app.NewPlacementReader(map[string]app.Placement{"host-a": {Kind: cliv1.PlacementKind_PLACEMENT_KIND_SSH, Probe: placementProbe{wait: true}}}, clock.NewManual(time.Unix(1, 0), 0), clock.Immediate{Longest: time.Minute}, time.Minute)
	got, err := reader.List(t.Context(), true)
	if err != nil {
		t.Fatal(err)
	}
	if got[0].GetUnknown().GetReason() != cliv1.OffersUnknownReason_OFFERS_UNKNOWN_REASON_TIMED_OUT || got[0].GetUnknown().GetDetail() == "" {
		t.Fatalf("got %v", got[0])
	}
}

type gatedProbe struct {
	entered chan<- struct{}
	release <-chan struct{}
}

func (p gatedProbe) Offers(ctx context.Context) ([]*cliv1.AcceleratorOffer, error) {
	p.entered <- struct{}{}
	select {
	case <-p.release:
		return nil, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func TestPlacementProbesRunInParallel(t *testing.T) {
	entered := make(chan struct{}, 2)
	release := make(chan struct{})
	probe := gatedProbe{entered: entered, release: release}
	reader := app.NewPlacementReader(map[string]app.Placement{
		"host-a": {Kind: cliv1.PlacementKind_PLACEMENT_KIND_SSH, Probe: probe},
		"host-b": {Kind: cliv1.PlacementKind_PLACEMENT_KIND_SSH, Probe: probe},
	}, clock.NewManual(time.Unix(1, 0), 0), clock.NewManualTimers(), time.Minute)
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { _, err := reader.List(ctx, true); done <- err }()
	// The deadline guards against a broken implementation hanging the test; it is not used to
	// decide the outcome of a successful probe.
	for range 2 {
		select {
		case <-entered:
		case <-ctx.Done():
			t.Fatal("probes did not both start")
		}
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

type countingProbe struct{ calls int }

func (p *countingProbe) Offers(context.Context) ([]*cliv1.AcceleratorOffer, error) {
	p.calls++
	return nil, nil
}
func TestPlacementReaderCachesUntilRefresh(t *testing.T) {
	probe := &countingProbe{}
	reader := app.NewPlacementReader(map[string]app.Placement{"host-a": {Kind: cliv1.PlacementKind_PLACEMENT_KIND_LOCAL, MaxJobs: 1, Probe: probe}}, clock.NewManual(time.Unix(1, 0), 0), clock.NewManualTimers(), time.Minute)
	first, err := reader.List(t.Context(), false)
	if err != nil {
		t.Fatal(err)
	}
	first[0].Name = "changed"
	cached, err := reader.List(t.Context(), false)
	if err != nil {
		t.Fatal(err)
	}
	if probe.calls != 1 || cached[0].Name != "host-a" {
		t.Fatalf("calls %d, cached %v", probe.calls, cached)
	}
	_, err = reader.List(t.Context(), true)
	if err != nil || probe.calls != 2 {
		t.Fatalf("refresh: %d calls, %v", probe.calls, err)
	}
}
