package daemontest

import (
	"sync"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// FakePlaceFit is a daemon.PlaceFit that says what a test scripted: a place fits a need when the test
// said so for its name, and fits nothing otherwise. It holds no rule of fit; the real one is the
// catalogue's.
type FakePlaceFit struct {
	mu    sync.Mutex
	fits  map[string]bool
	asked []core.AcceleratorNeed
}

var _ daemon.PlaceFit = (*FakePlaceFit)(nil)

// Set makes the named place fit every need, or none.
func (f *FakePlaceFit) Set(place string, fits bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fits == nil {
		f.fits = map[string]bool{}
	}
	f.fits[place] = fits
}

// Asked lists the needs put to it, in order.
func (f *FakePlaceFit) Asked() []core.AcceleratorNeed {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]core.AcceleratorNeed(nil), f.asked...)
}

// Fits implements daemon.PlaceFit.
func (f *FakePlaceFit) Fits(need core.AcceleratorNeed, place *pb.EvalPlace) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.asked = append(f.asked, need)
	return f.fits[place.GetName()]
}
