package daemon_test

import (
	"context"
	"testing"
	"time"

	"buf.build/go/protovalidate"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
)

type placementProbe struct{ calls int }

func (p *placementProbe) Offers(context.Context) ([]core.Accelerator, error) {
	p.calls++
	return []core.Accelerator{{Vendor: core.VendorNVIDIA, Model: core.ModelH100, Count: 2}}, nil
}

func TestListPlacementsServesTheReaderAndRefreshes(t *testing.T) {
	p := &placementProbe{}
	reader := app.NewPlacementReader(map[string]app.Placement{"host-a": {Kind: core.PlacementSSH, MaxJobs: 1, Source: core.OfferProbed, Probe: p}}, clock.NewManual(time.Unix(1, 0), 0), clock.NewManualTimers(), time.Minute)
	srv := daemon.NewServer(nil, "test", daemon.Inherited{}).WithPlacements(reader)
	for _, refresh := range []bool{false, false, true} {
		got, err := srv.ListPlacements(t.Context(), &cliv1.ListPlacementsRequest{Refresh: refresh})
		if err != nil {
			t.Fatal(err)
		}
		if err := protovalidate.Validate(got); err != nil {
			t.Fatal(err)
		}
		v := got.GetPlacements()[0]
		if len(got.GetPlacements()) != 1 || v.GetName() != "host-a" || v.GetKind() != cliv1.PlacementKind_PLACEMENT_KIND_SSH ||
			v.GetKnown().GetOffers()[0].GetCount() != 2 || v.GetKnown().GetOffers()[0].GetModel() != cliv1.AcceleratorModel_ACCELERATOR_MODEL_H100 {
			t.Fatalf("got %v", got)
		}
	}
	if p.calls != 2 {
		t.Fatalf("probed %d times, want 2", p.calls)
	}
}
