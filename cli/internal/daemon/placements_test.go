package daemon_test

import (
	"context"
	"testing"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
)

type placementProbe struct{ calls int }

func (p *placementProbe) Offers(context.Context) ([]*cliv1.AcceleratorOffer, error) {
	p.calls++
	return nil, nil
}
func TestListPlacementsServesTheReaderAndRefreshes(t *testing.T) {
	p := &placementProbe{}
	reader := app.NewPlacementReader(map[string]app.Placement{"host-a": {Kind: cliv1.PlacementKind_PLACEMENT_KIND_SSH, MaxJobs: 1, Source: cliv1.OfferSource_OFFER_SOURCE_PROBED, Probe: p}}, clock.NewManual(time.Unix(1, 0), 0), clock.NewManualTimers(), time.Minute)
	srv := daemon.NewServer(nil, "test", daemon.Inherited{}).WithPlacements(reader)
	for _, refresh := range []bool{false, false, true} {
		got, err := srv.ListPlacements(t.Context(), &cliv1.ListPlacementsRequest{Refresh: refresh})
		if err != nil {
			t.Fatal(err)
		}
		if len(got.GetPlacements()) != 1 || got.GetPlacements()[0].GetName() != "host-a" || got.GetPlacements()[0].GetKnown() == nil {
			t.Fatalf("got %v", got)
		}
	}
	if p.calls != 2 {
		t.Fatalf("probed %d times, want 2", p.calls)
	}
}
