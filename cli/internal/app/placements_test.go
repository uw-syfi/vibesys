package app_test

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

var oneH100 = []core.Accelerator{{Vendor: core.VendorNVIDIA, Model: core.ModelH100, Count: 1}}

// placement is a probed placement of the kind, with the probe.
func placement(kind core.PlacementKind, probe app.PlacementProbe) app.Placement {
	return app.Placement{Kind: kind, MaxJobs: 1, Probe: probe, Source: core.OfferProbed}
}

func newReader(placements map[string]app.Placement, timers clock.Timers) *app.PlacementReader {
	return app.NewPlacementReader(placements, clock.NewManual(time.Unix(1, 0), 0), timers, time.Minute)
}

type placementProbe struct {
	fail bool
	wait bool
}

func (p placementProbe) Offers(ctx context.Context) ([]core.Accelerator, error) {
	if p.wait {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	if p.fail {
		return nil, errors.New("unreadable")
	}
	return oneH100, nil
}

func TestPlacementReaderReportsEveryConfiguredPlacementAndOnlySuccessfulOffers(t *testing.T) {
	kinds := []core.PlacementKind{core.PlacementLocal, core.PlacementSSH, core.PlacementSlurm, core.PlacementApptainer}
	rng := rand.New(rand.NewPCG(1, 2))
	for range 200 {
		cfg := map[string]app.Placement{}
		for i := 0; i < rng.IntN(20); i++ {
			cfg[fmt.Sprintf("place-%d", i)] = placement(kinds[rng.IntN(len(kinds))], placementProbe{fail: rng.IntN(2) == 0})
		}
		got, err := newReader(cfg, clock.NewManualTimers()).List(t.Context(), true)
		if err != nil {
			t.Fatal(err)
		}
		if len(got) != len(cfg) {
			t.Fatalf("got %d placements, want %d", len(got), len(cfg))
		}
		seen := map[string]bool{}
		for _, v := range got {
			p, ok := cfg[v.Name]
			if !ok || seen[v.Name] || v.Kind != p.Kind {
				t.Fatalf("unexpected placement %v", v)
			}
			seen[v.Name] = true
			success := !p.Probe.(placementProbe).fail
			if (v.Unknown == core.OffersKnown) != success || v.Reachable != success || (success && len(v.Offers) != 1) || (!success && (len(v.Offers) != 0 || v.Detail == "")) {
				t.Fatalf("%s: %v", v.Name, v)
			}
		}
	}
}

func TestPlacementProbeTimeoutUsesInjectedTimer(t *testing.T) {
	reader := newReader(map[string]app.Placement{"host-a": placement(core.PlacementSSH, placementProbe{wait: true})}, clock.Immediate{Longest: time.Minute})
	got, err := reader.List(t.Context(), true)
	if err != nil {
		t.Fatal(err)
	}
	if got[0].Unknown != core.OffersTimedOut || got[0].Detail == "" {
		t.Fatalf("got %v", got[0])
	}
}

type gatedProbe struct {
	entered chan<- struct{}
	release <-chan struct{}
}

func (p gatedProbe) Offers(ctx context.Context) ([]core.Accelerator, error) {
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
	reader := newReader(map[string]app.Placement{
		"host-a": placement(core.PlacementSSH, probe),
		"host-b": placement(core.PlacementSSH, probe),
	}, clock.NewManualTimers())
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

func (p *countingProbe) Offers(context.Context) ([]core.Accelerator, error) {
	p.calls++
	return nil, nil
}

func TestPlacementReaderCachesUntilRefresh(t *testing.T) {
	probe := &countingProbe{}
	reader := newReader(map[string]app.Placement{"host-a": placement(core.PlacementLocal, probe)}, clock.NewManualTimers())
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

func TestDeclaredPlacementsReportTheirOffersAsDeclaredAndNoneAsNotProbeable(t *testing.T) {
	declared := []core.Accelerator{{Vendor: core.VendorAMD, Model: core.ModelMI300X, Count: 8}}
	reader := newReader(map[string]app.Placement{
		"cluster-a": {Kind: core.PlacementSlurm, Source: core.OfferDeclared, Probe: app.DeclaredOffers{Declared: declared}},
		"cluster-b": {Kind: core.PlacementApptainer, Source: core.OfferDeclared, Probe: app.DeclaredOffers{Err: fmt.Errorf("%w: no declared accelerators", app.ErrNotProbeable)}},
	}, clock.NewManualTimers())
	got, err := reader.List(t.Context(), false)
	if err != nil {
		t.Fatal(err)
	}
	a, b := got[0], got[1]
	if a.Unknown != core.OffersKnown || a.Source != core.OfferDeclared || len(a.Offers) != 1 || !a.ProbedAt.IsZero() {
		t.Errorf("declared: %v", a)
	}
	if b.Unknown != core.OffersNotProbeable || !strings.Contains(b.Detail, "no declared accelerators") {
		t.Errorf("none declared: %v", b)
	}
}
