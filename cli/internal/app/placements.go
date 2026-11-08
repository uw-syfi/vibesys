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

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// PlacementProbe reads the accelerators available to a job at a placement. It must return on cancellation.
type PlacementProbe interface {
	Offers(context.Context) ([]core.Accelerator, error)
}

// ErrNotProbeable marks a probe error as "the placement has nothing to read": its kind has no probe
// and the operator declared no offers.
var ErrNotProbeable = errors.New("not probeable")

// DeclaredOffers is the probe of a placement whose kind cannot be probed: it reports what the
// operator config declares, or Err (wrap ErrNotProbeable when nothing is declared).
type DeclaredOffers struct {
	Declared []core.Accelerator
	Err      error
}

// Offers implements PlacementProbe.
func (d DeclaredOffers) Offers(context.Context) ([]core.Accelerator, error) {
	return d.Declared, d.Err
}

// Placement describes the configured identity and its probe.
type Placement struct {
	Kind    core.PlacementKind
	MaxJobs uint32
	Probe   PlacementProbe
	// Source says how Probe learns the offers. It must be OfferProbed or OfferDeclared.
	Source core.OfferSource
}

// PlacementReader probes configured placements concurrently and caches the last completed observation.
type PlacementReader struct {
	placements map[string]Placement
	clock      clock.Clock
	timers     clock.Timers
	bound      time.Duration
	mu         sync.Mutex
	cached     []core.PlacementReading
}

// NewPlacementReader creates a reader with a per-placement bound.
func NewPlacementReader(placements map[string]Placement, c clock.Clock, timers clock.Timers, bound time.Duration) *PlacementReader {
	return &PlacementReader{placements: maps.Clone(placements), clock: c, timers: timers, bound: bound}
}

// List returns every configured placement, by name. The first call probes even if refresh is false.
func (r *PlacementReader) List(ctx context.Context, refresh bool) ([]core.PlacementReading, error) {
	r.mu.Lock()
	if !refresh && r.cached != nil {
		out := slices.Clone(r.cached)
		r.mu.Unlock()
		return out, nil
	}
	r.mu.Unlock()
	names := slices.Sorted(maps.Keys(r.placements))
	out := make([]core.PlacementReading, len(names))
	var wg sync.WaitGroup
	for i, name := range names {
		wg.Go(func() { out[i] = r.read(ctx, name, r.placements[name]) })
	}
	wg.Wait()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	r.mu.Lock()
	r.cached = slices.Clone(out)
	r.mu.Unlock()
	return out, nil
}

// read probes one placement within the bound.
func (r *PlacementReader) read(ctx context.Context, name string, p Placement) core.PlacementReading {
	view := core.PlacementReading{Name: name, Kind: p.Kind, MaxJobs: p.MaxJobs}
	bounded, stop := clock.Deadline(ctx, r.timers, r.bound)
	defer stop()
	offers, err := p.Probe.Offers(bounded)
	if bounded.Err() != nil {
		err = bounded.Err()
	}
	if err != nil {
		view.Unknown = core.OffersUnreadable
		switch {
		case errors.Is(context.Cause(bounded), context.DeadlineExceeded):
			view.Unknown = core.OffersTimedOut
		case errors.Is(err, ErrNotProbeable):
			view.Unknown = core.OffersNotProbeable
		}
		view.Detail = truncate(fmt.Sprintf("%s: %v", name, err), maxDetailBytes)
		view.Reason = view.Detail
		return view
	}
	view.Offers, view.Source = slices.Clone(offers), p.Source
	if p.Source == core.OfferDeclared {
		// Nothing was asked of the placement, so nothing is known of whether it answers.
		view.Reason = "not probed"
		return view
	}
	view.Reachable, view.ProbedAt = true, r.clock.Now()
	return view
}

// maxDetailBytes is the bound of the detail a client is given of a placement that could not be read.
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
