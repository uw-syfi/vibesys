package storetest

import (
	"math/rand/v2"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// startAs starts a workstream by the origin and returns what starting it came to.
func (e *env) startAs(title string, origin core.RecordOrigin) core.StartedWorkstream {
	e.t.Helper()
	started, err := e.s.StartWorkstream(e.ctx, core.NewWorkstream{Title: title, Origin: origin})
	if err != nil {
		e.t.Fatalf("StartWorkstream(%q): %v", title, err)
	}
	return started
}

func (e *env) start(title string) core.StartedWorkstream {
	e.t.Helper()
	return e.startAs(title, e.editOrigin("run-a", "lead"))
}

func (e *env) workstreams(filter core.WorkstreamFilter) []core.Workstream {
	e.t.Helper()
	got, err := e.s.Workstreams(e.ctx, filter)
	if err != nil {
		e.t.Fatalf("Workstreams(%+v): %v", filter, err)
	}
	return got
}

func (e *env) editWorkstream(n core.WorkstreamNumber, edit core.WorkstreamEdit) (core.Workstream, error) {
	if edit.At.IsZero() {
		edit.At = e.now()
	}
	return e.s.EditWorkstream(e.ctx, n, edit)
}

func setWorkstreamStatus(s core.WorkstreamStatus) core.WorkstreamEdit {
	return core.WorkstreamEdit{Status: &s}
}

// spellings are titles that name one line of work and differ in case and in white space, and so are
// one title once those are ignored.
var spellings = []string{"KV cache layout", "kv cache layout", "  KV   cache\tlayout ", "Kv CACHE Layout"}

func testWorkstreams(t *testing.T, newStore New) {
	t.Run("a store with no workstream lists none", func(t *testing.T) {
		e := newEnv(t, newStore)
		if got := e.workstreams(core.WorkstreamFilter{}); len(got) != 0 {
			t.Fatalf("Workstreams = %+v, want none", got)
		}
	})

	t.Run("numbers workstreams from 1, active, and keeps who started them", func(t *testing.T) {
		e := newEnv(t, newStore)
		titles := []string{"KV cache layout", "Batching", "Speculative decoding"}
		var made []core.Workstream
		for i, title := range titles {
			origin := e.editOrigin(core.RunID("run-"+title[:1]), "lead")
			started := e.startAs(title, origin)
			w := started.Workstream
			if !started.Created || w.Number != core.WorkstreamNumber(i+1) || w.Title != title || w.Status != core.WorkstreamActive || w.Note != "" ||
				!w.Origin.Same(origin) || !w.UpdatedAt.Equal(origin.At) {
				t.Fatalf("started %q = %+v, want number %d, active, made by %+v at its time", title, started, i+1, origin)
			}
			made = append(made, w)
		}
		if got := e.workstreams(core.WorkstreamFilter{}); !reflect.DeepEqual(got, made) {
			t.Fatalf("Workstreams = %+v, want %+v, by number", got, made)
		}
		if _, err := e.editWorkstream(2, setWorkstreamStatus(core.WorkstreamPaused)); err != nil {
			t.Fatal(err)
		}
		if _, err := e.editWorkstream(3, setWorkstreamStatus(core.WorkstreamClosed)); err != nil {
			t.Fatal(err)
		}
		numbers := func(f core.WorkstreamFilter) []core.WorkstreamNumber {
			var out []core.WorkstreamNumber
			for _, w := range e.workstreams(f) {
				out = append(out, w.Number)
			}
			return out
		}
		for _, tc := range []struct {
			filter core.WorkstreamFilter
			want   []core.WorkstreamNumber
		}{
			{core.WorkstreamFilter{Statuses: []core.WorkstreamStatus{core.WorkstreamActive}}, []core.WorkstreamNumber{1}},
			{core.WorkstreamFilter{Statuses: []core.WorkstreamStatus{core.WorkstreamPaused, core.WorkstreamClosed}}, []core.WorkstreamNumber{2, 3}},
			{core.WorkstreamFilter{Statuses: core.WorkstreamStatuses()}, []core.WorkstreamNumber{1, 2, 3}},
		} {
			if got := numbers(tc.filter); !slices.Equal(got, tc.want) {
				t.Errorf("filter %+v = %v, want %v", tc.filter, got, tc.want)
			}
		}
	})

	t.Run("starting a title that a workstream that is not closed has finds that one", func(t *testing.T) {
		e := newEnv(t, newStore)
		first := e.start(spellings[0])
		other := e.start("Batching")
		before := snapshot(t, e.s)
		for _, spelling := range spellings {
			again := e.start(core.NormalizeWorkstreamTitle(spelling))
			if again.Created || !reflect.DeepEqual(again.Workstream, first.Workstream) {
				t.Fatalf("starting %q = %+v, want the workstream that was started as %q, not created", spelling, again, spellings[0])
			}
		}
		unchanged(t, "starting an existing title", e.s, before)
		if _, err := e.editWorkstream(first.Workstream.Number, setWorkstreamStatus(core.WorkstreamPaused)); err != nil {
			t.Fatal(err)
		}
		if again := e.start(core.NormalizeWorkstreamTitle(spellings[2])); again.Created || again.Workstream.Number != first.Workstream.Number {
			t.Fatalf("starting the title of a paused workstream = %+v, want that workstream", again)
		}
		if _, err := e.editWorkstream(first.Workstream.Number, setWorkstreamStatus(core.WorkstreamClosed)); err != nil {
			t.Fatal(err)
		}
		reopened := e.start(core.NormalizeWorkstreamTitle(spellings[3]))
		if !reopened.Created || reopened.Workstream.Number != other.Workstream.Number+1 {
			t.Fatalf("starting the title of a closed workstream = %+v, want a new workstream", reopened)
		}
		if got := e.workstreams(core.WorkstreamFilter{}); len(got) != 3 {
			t.Fatalf("Workstreams = %d, want the closed one, the other and the new one", len(got))
		}
	})

	t.Run("an edit is one change, and one that changes nothing changes nothing", func(t *testing.T) {
		e := newEnv(t, newStore)
		w := e.start("KV cache layout").Workstream
		title, status, note := "Paged KV cache", core.WorkstreamPaused, "paused: accuracy gate"
		at := e.now()
		edited, err := e.s.EditWorkstream(e.ctx, w.Number, core.WorkstreamEdit{Title: &title, Status: &status, Note: &note, At: at})
		if err != nil {
			t.Fatal(err)
		}
		want := w
		want.Title, want.Status, want.Note, want.UpdatedAt = title, status, note, at
		if !reflect.DeepEqual(edited, want) {
			t.Fatalf("edited = %+v, want %+v: who started it stays", edited, want)
		}
		before := snapshot(t, e.s)
		same, err := e.s.EditWorkstream(e.ctx, w.Number, core.WorkstreamEdit{Title: &title, Status: &status, Note: &note, At: e.now()})
		if err != nil || !reflect.DeepEqual(same, edited) {
			t.Fatalf("an edit that sets what is there = %+v, %v; want the workstream as it was, update time included", same, err)
		}
		unchanged(t, "an edit that sets what is there", e.s, before)
		empty := ""
		cleared, err := e.editWorkstream(w.Number, core.WorkstreamEdit{Note: &empty})
		if err != nil || cleared.Note != "" || cleared.Title != title || cleared.Status != status {
			t.Fatalf("an empty note = %+v, %v; want the note cleared and the rest kept", cleared, err)
		}
	})

	t.Run("a title in use by another workstream that is not closed is a conflict", func(t *testing.T) {
		e := newEnv(t, newStore)
		a := e.start("KV cache layout").Workstream
		b := e.start("Batching").Workstream
		before := snapshot(t, e.s)
		taken := core.NormalizeWorkstreamTitle(spellings[3])
		_, err := e.editWorkstream(b.Number, core.WorkstreamEdit{Title: &taken})
		wantKind(t, "a title another workstream has", err, core.FailureConflict)
		unchanged(t, "a conflicting retitle", e.s, before)

		// A workstream may be renamed to its own title in another spelling, and a closed one's title is free.
		spelled := spellings[2]
		if _, err := e.editWorkstream(a.Number, core.WorkstreamEdit{Title: ptr(core.NormalizeWorkstreamTitle(spelled))}); err != nil {
			t.Fatalf("renaming a workstream to its own title in another spelling: %v", err)
		}
		if _, err := e.editWorkstream(a.Number, setWorkstreamStatus(core.WorkstreamClosed)); err != nil {
			t.Fatal(err)
		}
		if _, err := e.editWorkstream(b.Number, core.WorkstreamEdit{Title: &taken}); err != nil {
			t.Fatalf("taking the title of a closed workstream: %v", err)
		}
		_, err = e.editWorkstream(a.Number, setWorkstreamStatus(core.WorkstreamActive))
		wantKind(t, "reopening a workstream whose title is in use", err, core.FailureConflict)
	})

	t.Run("a refused call changes nothing and takes no number", func(t *testing.T) {
		e := newEnv(t, newStore)
		w := e.start("KV cache layout").Workstream
		before := snapshot(t, e.s)
		origin := e.editOrigin("run-a", "lead")
		long := strings.Repeat("é", core.MaxWorkstreamTitleRunes+1)
		for name, title := range map[string]string{
			"an empty title":                         "",
			"a blank title":                          "   ",
			"a title of two lines":                   "a\nb",
			"a title that is no text":                "bad\xff",
			"a title too long":                       long,
			"a title that is not in its stored form": " padded ",
			"a title with a run of spaces":           "two  spaces",
		} {
			_, err := e.s.StartWorkstream(e.ctx, core.NewWorkstream{Title: title, Origin: origin})
			wantKind(t, "StartWorkstream with "+name, err, core.FailureInvalidInput)
		}
		for name, o := range map[string]core.RecordOrigin{
			"no time":      {Run: "r"},
			"a long run":   {Run: core.RunID(strings.Repeat("x", core.MaxOriginBytes+1)), At: e.now()},
			"a long agent": {Agent: strings.Repeat("x", core.MaxOriginBytes+1), At: e.now()},
		} {
			_, err := e.s.StartWorkstream(e.ctx, core.NewWorkstream{Title: "Fresh", Origin: o})
			wantKind(t, "StartWorkstream with "+name, err, core.FailureInvalidInput)
		}
		bad := core.WorkstreamStatus("done")
		longNote, twoLines, noText := strings.Repeat("n", core.MaxWorkstreamNoteRunes+1), "a\nb", "bad\xff"
		unpadded, blank := " padded ", ""
		for name, edit := range map[string]core.WorkstreamEdit{
			"a status that does not exist": {Status: &bad},
			"a note too long":              {Note: &longNote},
			"a note of two lines":          {Note: &twoLines},
			"a note that is no text":       {Note: &noText},
			"a title not in stored form":   {Title: &unpadded},
			"an empty title":               {Title: &blank},
		} {
			edit.At = e.now()
			_, err := e.s.EditWorkstream(e.ctx, w.Number, edit)
			wantKind(t, "EditWorkstream with "+name, err, core.FailureInvalidInput)
		}
		_, err := e.s.EditWorkstream(e.ctx, w.Number, core.WorkstreamEdit{Note: ptr("n")})
		wantKind(t, "EditWorkstream with no time", err, core.FailureInvalidInput)
		_, err = e.editWorkstream(w.Number+9, core.WorkstreamEdit{Note: ptr("n")})
		wantKind(t, "an unknown workstream", err, core.FailureNotFound)
		_, err = e.editWorkstream(0, core.WorkstreamEdit{Note: ptr("n")})
		wantKind(t, "workstream 0", err, core.FailureNotFound)
		unchanged(t, "the refused calls", e.s, before)
		if next := e.start("Batching"); next.Workstream.Number != w.Number+1 {
			t.Errorf("the workstream after the refused ones is number %d, want %d", next.Workstream.Number, w.Number+1)
		}
	})

	t.Run("a plan joins a workstream the task has, and is moved to another", func(t *testing.T) {
		e := newEnv(t, newStore)
		a, b := e.start("KV cache layout").Workstream, e.start("Batching").Workstream
		origin := e.editOrigin("run-a", "lead")
		plan, err := e.s.CreatePlan(e.ctx, core.NewPlan{Title: "t", Workstream: a.Number, Origin: origin})
		if err != nil || plan.Workstream != a.Number {
			t.Fatalf("CreatePlan in workstream %d = %+v, %v", a.Number, plan, err)
		}
		move := b.Number
		moved, err := e.s.EditPlan(e.ctx, plan.Number, core.PlanEdit{Workstream: &move, Origin: e.editOrigin("run-b", "planner")})
		if err != nil || moved.Workstream != b.Number {
			t.Fatalf("EditPlan to workstream %d = %+v, %v", b.Number, moved, err)
		}
		if _, err := e.editWorkstream(b.Number, setWorkstreamStatus(core.WorkstreamClosed)); err != nil {
			t.Fatal(err)
		}
		if _, err := e.s.CreatePlan(e.ctx, core.NewPlan{Title: "t", Workstream: b.Number, Origin: e.editOrigin("r", "a")}); err != nil {
			t.Errorf("a plan joining a closed workstream: %v, want it accepted: a closed line of work still takes ideas", err)
		}
		kept, err := e.s.EditPlan(e.ctx, plan.Number, core.PlanEdit{Origin: e.editOrigin("r", "a")})
		if err != nil || kept.Workstream != b.Number {
			t.Errorf("an edit that names no workstream = %+v, %v; want it left where it was", kept, err)
		}
		before := snapshot(t, e.s)
		for _, n := range []core.WorkstreamNumber{3, 99} {
			_, err := e.s.CreatePlan(e.ctx, core.NewPlan{Title: "t", Workstream: n, Origin: e.editOrigin("r", "a")})
			wantKind(t, "CreatePlan in an unknown workstream", err, core.FailureNotFound)
			if err != nil && !strings.Contains(err.Error(), "--workstream") {
				t.Errorf("the refusal %q does not name --workstream", err)
			}
			_, err = e.s.EditPlan(e.ctx, plan.Number, core.PlanEdit{Workstream: &n, Origin: e.editOrigin("r", "a")})
			wantKind(t, "EditPlan to an unknown workstream", err, core.FailureNotFound)
		}
		zero := core.WorkstreamNumber(0)
		_, err = e.s.EditPlan(e.ctx, plan.Number, core.PlanEdit{Workstream: &zero, Origin: e.editOrigin("r", "a")})
		wantKind(t, "EditPlan to workstream 0, which no workstream has", err, core.FailureNotFound)
		unchanged(t, "plans naming a workstream the task lacks", e.s, before)
	})

	t.Run("a snapshot holds the workstreams", func(t *testing.T) {
		e := newEnv(t, newStore)
		w := e.start("KV cache layout").Workstream
		snap, err := e.s.Snapshot(e.ctx)
		if err != nil || !reflect.DeepEqual(snap.Workstreams, []core.Workstream{w}) {
			t.Fatalf("Snapshot.Workstreams = %+v, %v; want %+v", snap.Workstreams, err, w)
		}
	})

	t.Run("what a read returns is a copy", func(t *testing.T) {
		e := newEnv(t, newStore)
		e.start("KV cache layout")
		got := e.workstreams(core.WorkstreamFilter{})
		got[0].Title, got[0].Note = "changed", "changed"
		if again := e.workstreams(core.WorkstreamFilter{}); again[0].Title == "changed" || again[0].Note == "changed" {
			t.Fatalf("a change to the answer changed the store: %+v", again)
		}
	})

	// Whatever sequence of starts and edits is made, no two workstreams that are not closed share a
	// title, numbers have no gap, and each start answers as a simple model of the rule says: the
	// workstream that is not closed with the title, or else a new one with the next number.
	t.Run("starts and edits keep the titles of open workstreams apart", func(t *testing.T) {
		pool := []string{"KV cache", "kv  CACHE", "Batching", "batching ", "Paging", "Ünïcode ß", "ÜNÏCODE SS", "ünïcode ß"}
		statuses := core.WorkstreamStatuses()
		for seed := range uint64(6) {
			e := newEnv(t, newStore)
			rng := rand.New(rand.NewPCG(seed, 7))
			type entry struct {
				title  string
				status core.WorkstreamStatus
			}
			var model []entry
			named := func(title string) (core.WorkstreamNumber, bool) {
				for i, m := range model {
					if m.status != core.WorkstreamClosed && core.WorkstreamTitleKey(m.title) == core.WorkstreamTitleKey(title) {
						return core.WorkstreamNumber(i + 1), true
					}
				}
				return 0, false
			}
			for step := range 40 {
				if len(model) == 0 || rng.IntN(5) < 3 {
					title := core.NormalizeWorkstreamTitle(pool[rng.IntN(len(pool))])
					want, found := named(title)
					got := e.start(title)
					if found {
						if got.Created || got.Workstream.Number != want {
							t.Fatalf("seed %d step %d: start %q = %+v, want workstream %d, not created", seed, step, title, got, want)
						}
						continue
					}
					if !got.Created || got.Workstream.Number != core.WorkstreamNumber(len(model)+1) {
						t.Fatalf("seed %d step %d: start %q = %+v, want a new workstream %d", seed, step, title, got, len(model)+1)
					}
					model = append(model, entry{title, core.WorkstreamActive})
					continue
				}
				n := rng.IntN(len(model))
				edit := core.WorkstreamEdit{}
				next := model[n]
				if rng.IntN(2) == 0 {
					title := core.NormalizeWorkstreamTitle(pool[rng.IntN(len(pool))])
					edit.Title, next.title = &title, title
				}
				if rng.IntN(2) == 0 {
					status := statuses[rng.IntN(len(statuses))]
					edit.Status, next.status = &status, status
				}
				if edit.Title == nil && edit.Status == nil {
					continue
				}
				clash := false
				for i, m := range model {
					if i != n && m.status != core.WorkstreamClosed && core.WorkstreamTitleKey(m.title) == core.WorkstreamTitleKey(next.title) {
						clash = true
					}
				}
				clash = clash && next.status != core.WorkstreamClosed
				_, err := e.editWorkstream(core.WorkstreamNumber(n+1), edit)
				if clash {
					wantKind(t, "an edit that gives two open workstreams one title", err, core.FailureConflict)
					continue
				}
				if err != nil {
					t.Fatalf("seed %d step %d: edit %d: %v", seed, step, n+1, err)
				}
				model[n] = next
			}
			all := e.workstreams(core.WorkstreamFilter{})
			if len(all) != len(model) {
				t.Fatalf("seed %d: %d workstreams, want %d", seed, len(all), len(model))
			}
			for i, w := range all {
				if w.Number != core.WorkstreamNumber(i+1) || w.Title != model[i].title || w.Status != model[i].status {
					t.Fatalf("seed %d: workstream %d = %+v, want %+v", seed, i+1, w, model[i])
				}
			}
			if err := core.CheckWorkstreamTitles(all); err != nil {
				t.Fatalf("seed %d: %v", seed, err)
			}
		}
	})
}
