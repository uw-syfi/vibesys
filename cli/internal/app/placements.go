package app

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"slices"
	"sync"
	"time"
	"unicode/utf8"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// PlacementProbe reads the resources available to a job at a placement. It must return on cancellation.
type PlacementProbe interface {
	Offers(context.Context) ([]*cliv1.AcceleratorOffer, error)
}

// Placement describes the configured identity and its probe.
type Placement struct {
	Kind    cliv1.PlacementKind
	MaxJobs uint32
	Probe   PlacementProbe
}

// PlacementReader probes configured placements concurrently and caches the last completed observation.
type PlacementReader struct {
	placements map[string]Placement
	clock      clock.Clock
	timers     clock.Timers
	bound      time.Duration
	mu         sync.Mutex
	cached     []*cliv1.PlacementView
}

// NewPlacementReader creates a reader with a per-placement bound.
func NewPlacementReader(placements map[string]Placement, c clock.Clock, timers clock.Timers, bound time.Duration) *PlacementReader {
	return &PlacementReader{placements: maps.Clone(placements), clock: c, timers: timers, bound: bound}
}

// List returns every configured placement. The first call probes even if refresh is false.
func (r *PlacementReader) List(ctx context.Context, refresh bool) ([]*cliv1.PlacementView, error) {
	r.mu.Lock()
	if !refresh && r.cached != nil {
		out := clonePlacements(r.cached)
		r.mu.Unlock()
		return out, nil
	}
	r.mu.Unlock()
	names := slices.Sorted(maps.Keys(r.placements))
	out := make([]*cliv1.PlacementView, len(names))
	var wg sync.WaitGroup
	for i, name := range names {
		wg.Go(func() {
			p := r.placements[name]
			view := &cliv1.PlacementView{Name: name, Kind: p.Kind, MaxJobs: p.MaxJobs}
			bounded, stop := clock.Deadline(ctx, r.timers, r.bound)
			defer stop()
			offers, err := p.Probe.Offers(bounded)
			if bounded.Err() != nil {
				err = bounded.Err()
			}
			if err != nil {
				reason := cliv1.OffersUnknownReason_OFFERS_UNKNOWN_REASON_UNREADABLE
				if errors.Is(context.Cause(bounded), context.DeadlineExceeded) {
					reason = cliv1.OffersUnknownReason_OFFERS_UNKNOWN_REASON_TIMED_OUT
				}
				detail := fmt.Sprintf("%s: %v", name, err)
				if len(detail) > 1024 {
					detail = detail[:1024]
					for !utf8.ValidString(detail) {
						detail = detail[:len(detail)-1]
					}
				}
				view.Offers = &cliv1.PlacementView_Unknown{Unknown: &cliv1.OffersUnknown{Reason: reason, Detail: detail}}
				view.Reach = &cliv1.PlacementReach{Reason: detail}
			} else {
				view.Offers = &cliv1.PlacementView_Known{Known: &cliv1.PlacementOffers{Offers: offers, Source: cliv1.OfferSource_OFFER_SOURCE_PROBED, ProbedAt: timestamppb.New(r.clock.Now())}}
				view.Reach = &cliv1.PlacementReach{Reachable: true}
			}
			out[i] = view
		})
	}
	wg.Wait()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	r.mu.Lock()
	r.cached = clonePlacements(out)
	r.mu.Unlock()
	return out, nil
}

func clonePlacements(in []*cliv1.PlacementView) []*cliv1.PlacementView {
	out := make([]*cliv1.PlacementView, len(in))
	for i, v := range in {
		out[i] = proto.Clone(v).(*cliv1.PlacementView)
	}
	return out
}
