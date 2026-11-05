package storetest

import (
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// testFrontier covers FrontierStore: a frontier starts empty at revision zero, each admission is
// the next revision whole, one decided against another revision or by no decision changes nothing,
// and an admission of a member changes nothing.
func testFrontier(t *testing.T, newStore New) {
	t.Run("a store starts with an empty frontier", func(t *testing.T) {
		e := newEnv(t, newStore)
		got, err := e.s.Frontier(e.ctx)
		if err != nil || got.Revision != 0 || got.Members != nil {
			t.Fatalf("Frontier = %+v, %v; want empty at revision 0", got, err)
		}
	})

	t.Run("each admission is the next revision, whole", func(t *testing.T) {
		e := newEnv(t, newStore)
		g := newGen(11)
		var want core.Frontier
		for range 5 {
			members := g.members()
			got, err := e.s.UpdateFrontier(e.ctx, core.AssumeAdmitted(want.Revision, members))
			if err != nil {
				t.Fatalf("UpdateFrontier: %v", err)
			}
			want = core.Frontier{Revision: want.Revision + 1, Members: utc(members)}
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("UpdateFrontier = %+v, want %+v", got, want)
			}
			if read, err := e.s.Frontier(e.ctx); err != nil || !reflect.DeepEqual(read, want) {
				t.Fatalf("Frontier = %+v, %v; want %+v", read, err, want)
			}
		}
		read, _ := e.s.Frontier(e.ctx)
		read.Members[0].Commit = "changed"
		if again, _ := e.s.Frontier(e.ctx); !reflect.DeepEqual(again, want) {
			t.Fatal("changing a frontier that was read changed the store")
		}
	})

	t.Run("an admission against another revision is a conflict and changes nothing", func(t *testing.T) {
		e := newEnv(t, newStore)
		g := newGen(12)
		if _, err := e.s.UpdateFrontier(e.ctx, core.AssumeAdmitted(0, g.members())); err != nil {
			t.Fatal(err)
		}
		before := snapshot(t, e.s)
		for _, from := range []uint64{0, 2, 7} {
			_, err := e.s.UpdateFrontier(e.ctx, core.AssumeAdmitted(from, g.members()))
			wantKind(t, "an admission against another revision", err, core.FailureConflict)
		}
		unchanged(t, "admissions against another revision", e.s, before)
	})

	t.Run("what no store can hold is refused whole", func(t *testing.T) {
		e := newEnv(t, newStore)
		g := newGen(13)
		twice := g.members()
		twice = append(twice, twice[0])
		unnamed := g.members()
		unnamed[0].Commit = ""
		late := g.members()
		late[0].AddedAt = time.Date(10000, 1, 1, 0, 0, 0, 0, time.UTC)
		bad := g.members()
		bad[0].Commit = "bad\xff"
		before := snapshot(t, e.s)
		wantKind(t, "an admission no decision made", func() error { _, err := e.s.UpdateFrontier(e.ctx, core.FrontierAdmission{}); return err }(), core.FailureInvalidInput)
		for what, members := range map[string][]core.FrontierMember{
			"a commit twice": twice, "a member with no commit": unnamed, "a time past 9999": late, "a commit that is not UTF-8": bad,
		} {
			_, err := e.s.UpdateFrontier(e.ctx, core.AssumeAdmitted(0, members))
			wantKind(t, what, err, core.FailureInvalidInput)
		}
		unchanged(t, "refused admissions", e.s, before)
	})

	t.Run("a decision that the commit is a member already changes nothing", func(t *testing.T) {
		e := newEnv(t, newStore)
		at := e.now()
		first, err := e.s.UpdateFrontier(e.ctx, core.AssumeAdmitted(0, []core.FrontierMember{{Commit: commit(1), AddedAt: at}}))
		if err != nil {
			t.Fatal(err)
		}
		decision, err := core.DecideFrontier(core.FrontierRequest{Candidate: commit(1), Frontier: first, TaskVersion: "v1", At: e.now()})
		if err != nil {
			t.Fatal(err)
		}
		got, err := e.s.UpdateFrontier(e.ctx, decision.(core.FrontierAdmission))
		if err != nil || !reflect.DeepEqual(got, first) {
			t.Fatalf("UpdateFrontier = %+v, %v; want %+v unchanged", got, err, first)
		}
		if read, _ := e.s.Frontier(e.ctx); read.Revision != 1 {
			t.Fatalf("the revision moved to %d", read.Revision)
		}
	})

	t.Run("a decision over the store's own measurements is kept", func(t *testing.T) {
		e := newEnv(t, newStore)
		objectives := core.Objectives{
			Metrics:       []core.Metric{{Name: "tok_s", Direction: core.Maximize}, {Name: "ttft", Direction: core.Minimize}},
			RelativeNoise: 0.01,
		}
		five := func(v float64) []float64 { return []float64{v, v, v, v, v} }
		measure := func(c core.Commit, tok, ttft float64) {
			SeedMeasurement(t, e.s, Measurement{
				Commit: c, Samples: five(tok), At: e.now(), Objectives: objectives,
				Others: []core.Observation{{Name: "ttft", Values: five(ttft)}},
			})
		}
		measure(commit(1), 100, 100)
		measure(commit(2), 200, 200)
		measure(commit(3), 300, 100)
		for _, c := range []core.Commit{commit(1), commit(2), commit(3)} {
			frontier, err := e.s.Frontier(e.ctx)
			if err != nil {
				t.Fatal(err)
			}
			results, err := e.s.Results(e.ctx, core.ResultFilter{})
			if err != nil {
				t.Fatal(err)
			}
			decision, err := core.DecideFrontier(core.FrontierRequest{Candidate: c, Frontier: frontier, Results: results, TaskVersion: "v1", At: e.now()})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := e.s.UpdateFrontier(e.ctx, decision.(core.FrontierAdmission)); err != nil {
				t.Fatalf("UpdateFrontier(%.7s): %v", c, err)
			}
		}
		got, _ := e.s.Frontier(e.ctx)
		var commits []core.Commit
		for _, m := range got.Members {
			commits = append(commits, m.Commit)
		}
		if !slices.Equal(commits, []core.Commit{commit(3)}) || got.Revision != 3 {
			t.Fatalf("frontier %+v, want commit 3 alone at revision 3: it beats both", got)
		}
	})
}

// utc is members with their times in UTC, as a store returns them.
func utc(members []core.FrontierMember) []core.FrontierMember {
	out := slices.Clone(members)
	for i := range out {
		out[i].AddedAt = out[i].AddedAt.UTC()
	}
	return out
}
