package setup_test

import (
	"reflect"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/setup"
)

const run core.RunID = "run-1"

func facts(task string) setup.Read {
	f := stableFacts([]float64{1, 2, 3}, nil)
	f.Task = task
	return setup.Read{Task: task, Kind: setup.ReadFacts, Facts: f}
}

func TestAnOlderReadNeverOverwritesANewerOne(t *testing.T) {
	eachSeed(t, func(t *testing.T, seed uint64) {
		r := newRand(seed)
		reads := make([]setup.Read, 2+r.IntN(6))
		for i := range reads {
			f := genFacts(r)
			reads[i] = setup.Read{Seq: uint64(i + 1), Task: "t", Kind: pick(r, setup.ReadFacts, setup.ReadUnreadable), Facts: f, Detail: "half written"}
			if reads[i].Kind == setup.ReadUnreadable {
				reads[i].Facts = setup.Facts{}
			}
		}
		s := setup.NewStore()
		for _, i := range r.Perm(len(reads)) {
			s.Put(run, reads[i])
		}
		got, _ := s.Get(run)
		newest := reads[len(reads)-1]
		if got.Seq != newest.Seq || got.Readable != (newest.Kind == setup.ReadFacts) || !reflect.DeepEqual(got.Facts, newest.Facts) {
			t.Fatalf("held %+v, want the read numbered %d", got, newest.Seq)
		}
	})
}

func TestAReadThatChangesNothingMovesNoRevision(t *testing.T) {
	s := setup.NewStore()
	a := facts("t")
	a.Seq = 1
	if !s.Put(run, a) || s.Revision(run) != 1 {
		t.Fatal("first read not recorded")
	}
	a.Seq = 2
	if s.Put(run, a) || s.Revision(run) != 1 {
		t.Fatal("an equal read moved the revision")
	}
	if got, _ := s.Get(run); got.Seq != 2 {
		t.Fatalf("the sequence is not kept: %d", got.Seq)
	}
	a.Seq = 1
	if s.Put(run, a) {
		t.Fatal("an old read was taken")
	}
	b := facts("t")
	b.Seq = 3
	b.Facts.Version = "v2"
	if !s.Put(run, b) || s.Revision(run) != 2 {
		t.Fatal("a change was not recorded")
	}
}

func TestAFailedReadKeepsTheFactsAndASuccessClearsIt(t *testing.T) {
	s := setup.NewStore()
	a := facts("t")
	a.Seq = 1
	s.Put(run, a)
	rev := s.Revision(run)
	s.Put(run, setup.Read{Seq: 2, Task: "t", Kind: setup.ReadFailure, Detail: "daemon away"})
	got, _ := s.Get(run)
	if !got.Readable || got.Failure != "daemon away" || !reflect.DeepEqual(got.Facts, a.Facts) || got.Revision != rev+1 {
		t.Fatalf("%+v", got)
	}
	if s.Put(run, setup.Read{Seq: 3, Task: "t", Kind: setup.ReadFailure, Detail: "daemon away"}) {
		t.Fatal("the same failure moved the revision")
	}
	a.Seq = 4
	s.Put(run, a)
	if got, _ := s.Get(run); got.Failure != "" || !got.Readable {
		t.Fatalf("%+v", got)
	}
	s.Put(run, setup.Read{Seq: 5, Task: "renamed", Kind: setup.ReadFailure, Detail: "x"})
	if got, _ := s.Get(run); got.Readable || got.Task != "renamed" {
		t.Fatalf("facts of another task kept: %+v", got)
	}
}

func TestAnObservationDerivesPendingUntilItHasFacts(t *testing.T) {
	var none setup.Observation
	if got := none.Derived(setup.Standard); len(got.Unmet()) != 6 {
		t.Fatalf("%+v", got)
	}
	s := setup.NewStore()
	s.Put(run, setup.Read{Seq: 1, Task: "t", Kind: setup.ReadUnreadable, Detail: "half written"})
	o, _ := s.Get(run)
	if got := o.Derived(setup.Standard); got.Checks[0].Detail != "half written" || len(got.Unmet()) != 6 {
		t.Fatalf("%+v", got)
	}
	s.Forget(run)
	if _, ok := s.Get(run); ok || s.Revision(run) != 0 {
		t.Fatal("not forgotten")
	}
}
