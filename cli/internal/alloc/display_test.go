package alloc_test

import (
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
)

// A thousand looks at a pending allocation that change only what the backend says for display (why
// it waits, when it expects to start) are not durable steps: the shell logs none of them, while the
// state shows the latest words. A look that changes the allocation is durable.
func TestLooksThatChangeOnlyTheDisplayWordsAreNotDurable(t *testing.T) {
	w := newWorld(t)
	id := w.ask(gpu, time.Hour)
	w.took(id)
	for i := range 1000 {
		w.pass(5 * time.Second)
		before := w.s
		seen := alloc.SeenPending{Reason: []string{"Priority", "Resources"}[i%2], EstimatedStart: w.now.Add(time.Duration(i) * time.Minute)}
		w.do(alloc.Observed{ID: id, Seen: seen})
		if alloc.Durable(before, w.s) {
			t.Fatalf("look %d, which changed only the display words, is durable", i)
		}
		if p := w.get(id).Phase.(alloc.Pending); p.Reason != seen.Reason || !p.EstimatedStart.Equal(seen.EstimatedStart) {
			t.Fatalf("look %d: the allocation shows %+v, want %+v", i, p, seen)
		}
	}
	before := w.s
	w.do(alloc.Observed{ID: id, Seen: alloc.SeenRunning{Node: "n1"}})
	if !alloc.Durable(before, w.s) {
		t.Fatal("the look that saw the allocation start is not durable")
	}
}
