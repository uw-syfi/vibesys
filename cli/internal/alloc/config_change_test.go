package alloc_test

import (
	"maps"
	"math/rand/v2"
	"reflect"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
)

// changed is the package's config with one placement's limits changed, or the placement removed.
func changed(placement string, change func(*alloc.Limits)) alloc.Config {
	out := alloc.Config{Placements: maps.Clone(config.Placements)}
	if change == nil {
		delete(out.Placements, placement)
		return out
	}
	l := out.Placements[placement]
	change(&l)
	out.Placements[placement] = l
	return out
}

// The reviewer's histories: decisions taken under one config, replayed after the operator changed
// it. A log that records the config it was decided under folds to what was decided, whatever the
// config is now: a lowered max_held refuses no Ask that was accepted (so no successor is renumbered
// and no live allocation is left without a record), and a changed fit_margin moves no job. Before,
// Fold replayed the inputs under today's config.
func TestALoggedHistoryFoldsTheSameUnderAChangedConfig(t *testing.T) {
	w := newWorld(t)
	w.do(alloc.DaemonStarted{Config: config})
	first := w.running(gpu, 2*time.Hour)
	second := w.running(gpu, 2*time.Hour)
	// A job that fits with the margin of 5m and would not with one of 50m.
	w.submit("j1", 2*time.Hour-10*time.Minute, first)
	want := w.s
	for name, cfg := range map[string]alloc.Config{
		"max_held lowered to 1":  changed(gpu, func(l *alloc.Limits) { l.MaxHeld = 1 }),
		"fit_margin raised":      changed(gpu, func(l *alloc.Limits) { l.Margin = 50 * time.Minute }),
		"max_for lowered":        changed(gpu, func(l *alloc.Limits) { l.MaxFor = time.Hour }),
		"allocations removed":    changed(gpu, nil),
		"another placement gone": changed(queue, nil),
	} {
		got, _ := alloc.Fold(cfg, w.log)
		if !reflect.DeepEqual(got, want) {
			t.Errorf("%s: the fold is\n%+v\nwant\n%+v", name, got.Allocations(), want.Allocations())
		}
	}
	if _, ok := want.Get(second); !ok {
		t.Fatal("the history has no second allocation")
	}
}

// A start with a config whose allocations section of a placement was removed ends that
// placement's allocations, EndExternal, and asks for their release; the others are unchanged.
func TestAStartWithoutAPlacementsSectionEndsAndReleasesItsAllocations(t *testing.T) {
	w := newWorld(t)
	w.do(alloc.DaemonStarted{Config: config})
	gone := w.running(gpu, time.Hour)
	kept := w.running(local, time.Hour)
	keptBefore := w.get(kept)
	w.cfg = changed(gpu, nil) // The shell steps every input after a start under the config it started with.
	actions := w.do(alloc.DaemonStarted{Config: w.cfg})
	if end := w.ended(gone); end.Reason != alloc.EndExternal || end.Released {
		t.Fatalf("the allocation of the removed placement ended %+v, want external and its release asked", end)
	}
	if !has(actions, alloc.Release{ID: gone, Handle: w.get(gone).Handle}) {
		t.Fatalf("the start asked %v, want the release of %s", kinds(actions), gone)
	}
	if got := w.get(kept); !reflect.DeepEqual(got, keptBefore) {
		t.Fatalf("the allocation of a placement that kept its section changed: %+v, was %+v", got, keptBefore)
	}
	// What is decided after the start is decided under its config: the removed placement takes no
	// Ask, and a fold of the log replays it so.
	w.refuse(alloc.Ask{Run: "run-a", Placement: gpu, For: time.Hour}, alloc.NotEnabled{})
	if got, _ := alloc.Fold(config, w.log); !reflect.DeepEqual(got, w.s) {
		t.Fatal("the fold of the log is not the state the steps made")
	}
}

// randomConfig is the package's config with random limits lowered, raised or removed.
func randomConfig(r *rand.Rand) alloc.Config {
	out := alloc.Config{Placements: map[string]alloc.Limits{}}
	for name, l := range config.Placements {
		if r.IntN(4) == 0 {
			continue
		}
		l.MaxHeld = r.IntN(3)
		l.MaxFor = time.Duration(1+r.IntN(24)) * time.Hour
		l.Margin = time.Duration(r.IntN(60)) * time.Minute
		if !l.Endless && r.IntN(2) == 0 {
			l.QueueTimeout = time.Duration(r.IntN(12)) * time.Hour
		}
		out.Placements[name] = l
	}
	return out
}

// For random histories that start with the config they ran under, and random later configs, the
// fold of the log is the history's state whatever config it is folded with, and a start with the
// later config changes no allocation of a placement it still offers and ends every one, not ended
// already, of a placement it does not. No record is renumbered, dropped or refused.
func TestRandomHistoriesFoldTheSameUnderEveryLaterConfig(t *testing.T) {
	for seed := range 40 {
		r := rand.New(rand.NewPCG(uint64(seed), 11))
		log := append([]alloc.Input{{At: epoch, Event: alloc.DaemonStarted{Config: config}}}, replay(rand.New(rand.NewPCG(uint64(seed), 7)), 300)...)
		want, _ := alloc.Fold(config, log)
		for range 5 {
			later := randomConfig(r)
			got, _ := alloc.Fold(later, log)
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("seed %d: folding under %+v gave another state", seed, later)
			}
			at := log[len(log)-1].At.Add(time.Minute)
			after, _ := alloc.Step(got, later, at, alloc.DaemonStarted{Config: later})
			for _, a := range want.Allocations() {
				b, ok := after.Get(a.ID)
				_, offered := later.Placements[a.Placement]
				switch {
				case !ok:
					t.Fatalf("seed %d: the start dropped %s", seed, a.ID)
				case offered && !reflect.DeepEqual(a.Phase, b.Phase):
					t.Fatalf("seed %d: the start changed %s of a placement it still offers: %+v, was %+v", seed, a.ID, b.Phase, a.Phase)
				case !offered && b.Stage() != alloc.StageEnded:
					t.Fatalf("seed %d: %s of a removed placement is %s after the start", seed, a.ID, b.Stage())
				}
			}
		}
	}
}
