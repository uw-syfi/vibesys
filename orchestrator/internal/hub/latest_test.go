package hub_test

import (
	"fmt"
	"math/rand"
	"testing"
	"testing/quick"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
)

func TestLatestNeverLagsAndNeverRegresses(t *testing.T) {
	property := func(history []uint16) bool {
		h := hub.New(hub.Limits{MaxItems: 1, MaxBytes: 1})
		s := h.Subscribe(hub.Interest{Everything: true})
		defer s.Close()
		want := map[string]uint64{}
		seen := map[string]uint64{}
		for i, n := range history {
			run := fmt.Sprint(n % 7)
			rev := uint64(n) + 1
			h.PublishLatest(core.RunID(run), &pb.SearchState{RunId: run, Revision: rev})
			want[run] = max(want[run], rev)
			if i%11 == 0 {
				msgs, lag := s.Take()
				if lag {
					return false
				}
				for _, m := range msgs {
					if m.Search.Revision <= seen[m.Search.RunId] {
						return false
					}
					seen[m.Search.RunId] = m.Search.Revision
				}
			}
		}
		msgs, lag := s.Take()
		if lag {
			return false
		}
		for _, m := range msgs {
			seen[m.Search.RunId] = m.Search.Revision
		}
		for run, rev := range want {
			if seen[run] != rev {
				return false
			}
		}
		return true
	}
	if err := quick.Check(property, &quick.Config{Rand: rand.New(rand.NewSource(1))}); err != nil {
		t.Fatal(err)
	}
}

func TestLatestThousandPublishesAndLag(t *testing.T) {
	h := hub.New(hub.Limits{MaxItems: 1, MaxBytes: 100})
	s := h.Subscribe(hub.Interest{Everything: true})
	defer s.Close()
	agent := h.Subscribe(hub.Interest{Agent: "a"})
	defer agent.Close()
	for rev := uint64(1); rev <= 1000; rev++ {
		for _, run := range []core.RunID{"a", "b"} {
			h.PublishLatest(run, &pb.SearchState{RunId: string(run), Revision: rev})
		}
	}
	msgs, lag := s.Take()
	if lag || len(msgs) != 2 {
		t.Fatal(len(msgs), lag)
	}
	for _, m := range msgs {
		if m.Search.Revision != 1000 {
			t.Fatal(m)
		}
	}
	if msgs, lag := agent.Take(); lag || len(msgs) != 0 {
		t.Fatal(msgs, lag)
	}
	h.PublishStored(hub.Stored{Change: &pb.RunChange{Sequence: 1}})
	h.PublishStored(hub.Stored{Change: &pb.RunChange{Sequence: 2}})
	h.PublishLatest("a", &pb.SearchState{RunId: "a", Revision: 1001})
	if _, lag := s.Take(); !lag {
		t.Fatal("expected stored lag")
	}
	s.Resume()
	msgs, lag = s.Take()
	if lag || len(msgs) != 1 || msgs[0].Search.Revision != 1001 {
		t.Fatal(msgs, lag)
	}
	h.PublishLatest("a", &pb.SearchState{RunId: "a", Revision: 999})
	if msgs, _ := s.Take(); len(msgs) != 0 {
		t.Fatal("older revision replaced latest")
	}
}

func TestSetupNotesNeverLagAndNeverRegress(t *testing.T) {
	property := func(history []uint16) bool {
		// One item and one byte of room: a stored event lags this subscription at once, and a note must not.
		h := hub.New(hub.Limits{MaxItems: 1, MaxBytes: 1})
		s := h.Subscribe(hub.Interest{Everything: true})
		defer s.Close()
		other := h.Subscribe(hub.Interest{Run: "0"})
		defer other.Close()
		want := map[core.RunID]uint64{}
		seen := map[core.RunID]uint64{}
		take := func() bool {
			msgs, lag := s.Take()
			if lag {
				return false
			}
			for _, m := range msgs {
				if m.Setup.Revision <= seen[m.Setup.Run] {
					return false
				}
				seen[m.Setup.Run] = m.Setup.Revision
			}
			return true
		}
		for i, n := range history {
			run := core.RunID(fmt.Sprint(n % 7))
			rev := uint64(n) + 1
			h.PublishSetup(run, rev)
			want[run] = max(want[run], rev)
			if i%11 == 0 && !take() {
				return false
			}
		}
		if !take() {
			return false
		}
		for run, rev := range want {
			if seen[run] != rev {
				return false
			}
		}
		// Only a watcher of everything is told: one scoped to a run gets stored events alone.
		msgs, _ := other.Take()
		return len(msgs) == 0
	}
	if err := quick.Check(property, &quick.Config{Rand: rand.New(rand.NewSource(2))}); err != nil {
		t.Fatal(err)
	}
}
