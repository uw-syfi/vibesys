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

// ErrNotProbeable marks a probe error as "the placement has nothing to read": its kind has no probe
// and the operator declared no offers.
var ErrNotProbeable = errors.New("not probeable")

// DeclaredOffers is the probe of a placement whose kind cannot be probed: it reports what the
// operator config declares, or Err (wrap ErrNotProbeable when nothing is declared).
type DeclaredOffers struct {
	Declared []*cliv1.AcceleratorOffer
	Err      error
}

// Offers implements PlacementProbe.
func (d DeclaredOffers) Offers(context.Context) ([]*cliv1.AcceleratorOffer, error) {
	return d.Declared, d.Err
}

// Placement describes the configured identity and its probe.
type Placement struct {
	Kind    cliv1.PlacementKind
	MaxJobs uint32
	Probe   PlacementProbe
	// Source says how Probe learns the offers. It must be PROBED or DECLARED.
	Source cliv1.OfferSource
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
			out[i] = r.read(ctx, name, r.placements[name])
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

// read probes one placement within the bound.
func (r *PlacementReader) read(ctx context.Context, name string, p Placement) *cliv1.PlacementView {
	view := &cliv1.PlacementView{Name: name, Kind: p.Kind, MaxJobs: p.MaxJobs}
	bounded, stop := clock.Deadline(ctx, r.timers, r.bound)
	defer stop()
	offers, err := p.Probe.Offers(bounded)
	if bounded.Err() != nil {
		err = bounded.Err()
	}
	if err != nil {
		reason := cliv1.OffersUnknownReason_OFFERS_UNKNOWN_REASON_UNREADABLE
		switch {
		case errors.Is(context.Cause(bounded), context.DeadlineExceeded):
			reason = cliv1.OffersUnknownReason_OFFERS_UNKNOWN_REASON_TIMED_OUT
		case errors.Is(err, ErrNotProbeable):
			reason = cliv1.OffersUnknownReason_OFFERS_UNKNOWN_REASON_NOT_PROBEABLE
		}
		detail := truncate(fmt.Sprintf("%s: %v", name, err), maxDetailBytes)
		view.Offers = &cliv1.PlacementView_Unknown{Unknown: &cliv1.OffersUnknown{Reason: reason, Detail: detail}}
		view.Reach = &cliv1.PlacementReach{Reason: detail}
		return view
	}
	known := &cliv1.PlacementOffers{Offers: offers, Source: p.Source}
	view.Reach = &cliv1.PlacementReach{Reachable: true}
	if p.Source == cliv1.OfferSource_OFFER_SOURCE_DECLARED {
		// Nothing was asked of the placement, so nothing is known of whether it answers.
		view.Reach = &cliv1.PlacementReach{Reason: "not probed"}
	} else {
		known.ProbedAt = timestamppb.New(r.clock.Now())
	}
	view.Offers = &cliv1.PlacementView_Known{Known: known}
	return view
}

// maxDetailBytes is the bound of OffersUnknown.detail and PlacementReach.reason in the contract.
const maxDetailBytes = 1024

// truncate cuts s to at most n bytes without splitting a rune.
func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	s = s[:n]
	for !utf8.ValidString(s) {
		s = s[:len(s)-1]
	}
	return s
}

func clonePlacements(in []*cliv1.PlacementView) []*cliv1.PlacementView {
	out := make([]*cliv1.PlacementView, len(in))
	for i, v := range in {
		out[i] = proto.Clone(v).(*cliv1.PlacementView)
	}
	return out
}
