package storetest

import (
	"bytes"
	"math/rand/v2"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func testPlans(t *testing.T, newStore New) {
	t.Run("numbers plans from 1, opens them, and never reuses a number", func(t *testing.T) {
		e := newEnv(t, newStore)
		for i := 1; i <= 5; i++ {
			at := e.now()
			plan, err := e.s.CreatePlan(e.ctx, core.NewPlan{Title: "t", Body: "b", At: at})
			if err != nil {
				t.Fatal(err)
			}
			if plan.Number != core.PlanNumber(i) || plan.Status != core.PlanOpen || !plan.CreatedAt.Equal(at) || !plan.UpdatedAt.Equal(at) || len(plan.Comments) != 0 {
				t.Fatalf("plan %d = %+v, want number %d, open, created and updated at %v", i, plan, i, at)
			}
			if i == 3 {
				// A refused create takes no number.
				_, err := e.s.CreatePlan(e.ctx, core.NewPlan{Title: "bad\xff", At: e.now()})
				wantKind(t, "a title that is not UTF-8", err, core.FailureInvalidInput)
			}
		}
		plans, err := e.s.Plans(e.ctx, core.PlanFilter{})
		if err != nil || len(plans) != 5 {
			t.Fatalf("Plans = %d, %v; want 5", len(plans), err)
		}
		for i, plan := range plans {
			if plan.Number != core.PlanNumber(i+1) {
				t.Errorf("plan %d has number %d: numbers go in order, without gaps", i, plan.Number)
			}
		}
	})

	t.Run("an edit is one change", func(t *testing.T) {
		e := newEnv(t, newStore)
		plan := e.createPlan("first")
		status, title := core.PlanChosen, "renamed"
		comment := core.PlanComment{Text: "chosen because it is cheap", At: e.now(), Agent: "lead"}
		editAt := e.now()
		edited, err := e.s.EditPlan(e.ctx, plan.Number, core.PlanEdit{Title: &title, Status: &status, Comment: &comment, At: editAt})
		if err != nil {
			t.Fatal(err)
		}
		want := plan
		want.Title, want.Status, want.Comments, want.UpdatedAt = title, status, []core.PlanComment{comment}, editAt
		if !reflect.DeepEqual(edited, want) {
			t.Errorf("edited plan = %+v, want %+v: a field left nil stays as it was", edited, want)
		}
		second := core.PlanComment{Text: "it won", At: e.now()}
		won := core.PlanWon
		edited, err = e.s.EditPlan(e.ctx, plan.Number, core.PlanEdit{Status: &won, Comment: &second, At: e.now()})
		if err != nil {
			t.Fatal(err)
		}
		if edited.Status != core.PlanWon || !reflect.DeepEqual(edited.Comments, []core.PlanComment{comment, second}) || edited.Title != title || edited.Body != plan.Body {
			t.Errorf("plan = %+v, want comments in order and the title and body kept", edited)
		}
		got, err := e.s.Plan(e.ctx, plan.Number)
		if err != nil || !reflect.DeepEqual(got, edited) {
			t.Errorf("Plan = %+v, %v; want %+v with its comments", got, err, edited)
		}
	})

	t.Run("a refused edit changes nothing", func(t *testing.T) {
		e := newEnv(t, newStore)
		plan := e.createPlan("first")
		before := snapshot(t, e.s)
		title, bad := "kept?", "bad\xff"
		status := core.PlanParked
		_, err := e.s.EditPlan(e.ctx, plan.Number, core.PlanEdit{Title: &title, Status: &status, Comment: &core.PlanComment{Text: bad}, At: e.now()})
		wantKind(t, "a comment that is not UTF-8", err, core.FailureInvalidInput)
		_, err = e.s.EditPlan(e.ctx, plan.Number+9, core.PlanEdit{Title: &title, At: e.now()})
		wantKind(t, "an unknown plan", err, core.FailureNotFound)
		unchanged(t, "a refused edit", e.s, before)
	})

	t.Run("fails with not found for a plan that does not exist", func(t *testing.T) {
		e := newEnv(t, newStore)
		e.createPlan("one")
		for _, n := range []core.PlanNumber{0, 2, 99} {
			_, err := e.s.Plan(e.ctx, n)
			wantKind(t, "Plan", err, core.FailureNotFound)
		}
	})

	t.Run("filters by status and text, by number", func(t *testing.T) {
		e := newEnv(t, newStore)
		rng := rand.New(rand.NewPCG(5, 5))
		titles := []string{"batch the writes", "shard the index", "Batch reads", "cache"}
		bodies := []string{"use a ring buffer", "hash by prefix", "", "LRU"}
		statuses := []core.PlanStatus{core.PlanOpen, core.PlanChosen, core.PlanWon, core.PlanParked, core.PlanRejected}
		for range 16 {
			plan, err := e.s.CreatePlan(e.ctx, core.NewPlan{Title: titles[rng.IntN(4)], Body: bodies[rng.IntN(4)], At: e.now()})
			if err != nil {
				t.Fatal(err)
			}
			st := statuses[rng.IntN(5)]
			if _, err := e.s.EditPlan(e.ctx, plan.Number, core.PlanEdit{Status: &st, At: e.now()}); err != nil {
				t.Fatal(err)
			}
		}
		all, err := e.s.Plans(e.ctx, core.PlanFilter{})
		if err != nil || len(all) != 16 {
			t.Fatalf("Plans = %d, %v", len(all), err)
		}
		for range 40 {
			var f core.PlanFilter
			if rng.IntN(2) == 0 {
				f.Statuses = []core.PlanStatus{statuses[rng.IntN(5)], statuses[rng.IntN(5)]}
			}
			if rng.IntN(2) == 0 {
				f.Text = []string{"batch", "Batch", "ring", "prefix", "LRU", "zzz"}[rng.IntN(6)]
			}
			var want []core.Plan
			for _, p := range all {
				statusOK := len(f.Statuses) == 0 || slices.Contains(f.Statuses, p.Status)
				textOK := f.Text == "" || strings.Contains(p.Title, f.Text) || strings.Contains(p.Body, f.Text)
				if statusOK && textOK {
					want = append(want, p)
				}
			}
			if got, err := e.s.Plans(e.ctx, f); err != nil || !reflect.DeepEqual(got, want) {
				t.Fatalf("Plans(%+v) = %d plans, %v; want %d", f, len(got), err, len(want))
			}
		}
	})
}

func testEvents(t *testing.T, newStore New) {
	draft := func(e *env, run core.RunID, command core.Command) core.EventDraft {
		return core.EventDraft{SchemaVersion: 1, Run: run, HostID: "host", Task: "kv-store", Commit: commit(1), Harness: "claude-code", Agent: "a", At: e.now(), Command: command, Outcome: core.OutcomeSucceeded}
	}

	t.Run("gives each run positions from 1 without gaps", func(t *testing.T) {
		e := newEnv(t, newStore)
		for i := 1; i <= 4; i++ {
			for _, run := range []core.RunID{"run-a", "run-b"} {
				ev, err := e.s.AppendEvent(e.ctx, draft(e, run, core.CommandWait))
				if err != nil || ev.Sequence != uint64(i) || ev.Run != run {
					t.Fatalf("event %d of %s = %+v, %v; want position %d", i, run, ev, err, i)
				}
			}
		}
		got, err := e.s.Events(e.ctx, "run-a", 0, 0)
		if err != nil || len(got) != 4 {
			t.Fatalf("Events = %d, %v; want 4", len(got), err)
		}
		for i, ev := range got {
			if ev.Sequence != uint64(i+1) || ev.Run != "run-a" {
				t.Errorf("event %d = %+v, want position %d of run-a", i, ev, i+1)
			}
		}
	})

	t.Run("a refused event takes no position", func(t *testing.T) {
		e := newEnv(t, newStore)
		if _, err := e.s.AppendEvent(e.ctx, draft(e, "run-a", core.CommandWait)); err != nil {
			t.Fatal(err)
		}
		bad := draft(e, "run-a", core.CommandWait)
		bad.Agent = "bad\xff"
		_, err := e.s.AppendEvent(e.ctx, bad)
		wantKind(t, "an agent that is not UTF-8", err, core.FailureInvalidInput)
		for _, run := range []core.RunID{"", "a/b", "..", "run a"} {
			_, err := e.s.AppendEvent(e.ctx, draft(e, run, core.CommandWait))
			wantKind(t, "run "+string(run), err, core.FailureInvalidInput)
		}
		if ev, err := e.s.AppendEvent(e.ctx, draft(e, "run-a", core.CommandNote)); err != nil || ev.Sequence != 2 {
			t.Errorf("next event = %+v, %v; want position 2", ev, err)
		}
	})

	t.Run("reads after a position, up to a limit", func(t *testing.T) {
		e := newEnv(t, newStore)
		for range 7 {
			if _, err := e.s.AppendEvent(e.ctx, draft(e, "run-a", core.CommandJobs)); err != nil {
				t.Fatal(err)
			}
		}
		cases := []struct {
			after uint64
			limit int
			first uint64
			count int
		}{
			{0, 0, 1, 7}, {0, 3, 1, 3}, {3, 0, 4, 4}, {3, 2, 4, 2}, {6, 5, 7, 1}, {7, 0, 0, 0}, {7, 3, 0, 0}, {90, 0, 0, 0}, {0, 100, 1, 7},
		}
		for _, c := range cases {
			got, err := e.s.Events(e.ctx, "run-a", c.after, c.limit)
			if err != nil || len(got) != c.count || (c.count > 0 && got[0].Sequence != c.first) {
				t.Errorf("Events(after %d, limit %d) = %d events from %d, %v; want %d from %d", c.after, c.limit, len(got), firstOf(got), err, c.count, c.first)
			}
			for i, ev := range got {
				if ev.Sequence != got[0].Sequence+uint64(i) {
					t.Errorf("Events(after %d, limit %d) is not in order: %+v", c.after, c.limit, got)
				}
			}
		}
		if got, err := e.s.Events(e.ctx, "no-such-run", 0, 0); err != nil || len(got) != 0 {
			t.Errorf("Events of an unknown run = %d, %v; want none and no error", len(got), err)
		}
		_, err := e.s.Events(e.ctx, "run-a", 0, -1)
		wantKind(t, "a negative limit", err, core.FailureInvalidInput)
	})

	t.Run("keeps every kind of detail as it was", func(t *testing.T) {
		e := newEnv(t, newStore)
		g := newGen(1)
		for i, detail := range g.details() {
			d := draft(e, "run-a", core.CommandShow)
			d.Detail, d.Job = detail, "j1"
			ev, err := e.s.AppendEvent(e.ctx, d)
			if err != nil {
				t.Fatalf("detail %d (%T): %v", i, detail, err)
			}
			if !reflect.DeepEqual(ev.EventDraft, d) {
				t.Errorf("detail %d (%T): AppendEvent kept %+v, want %+v", i, detail, ev.EventDraft, d)
			}
			got, err := e.s.Events(e.ctx, "run-a", uint64(i), 1)
			if err != nil || len(got) != 1 || !reflect.DeepEqual(got[0], ev) {
				t.Errorf("detail %d (%T): Events = %+v, %v; want %+v", i, detail, got, err, ev)
			}
		}
	})
}

func firstOf(events []core.Event) uint64 {
	if len(events) == 0 {
		return 0
	}
	return events[0].Sequence
}

func testLogs(t *testing.T, newStore New) {
	t.Run("a job has a log from the moment it exists", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.command(1, "ls")).Job
		chunk, err := e.s.ReadLog(e.ctx, job.ID, 0, 0)
		if err != nil || len(chunk.Data) != 0 || chunk.Next != 0 {
			t.Errorf("ReadLog of a job with no output = %+v, %v; want nothing at offset 0", chunk, err)
		}
	})

	t.Run("reads from an offset and says where to continue", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.command(1, "ls")).Job
		for _, part := range []string{"hello ", "", "wor", "ld"} {
			if err := e.s.AppendLog(e.ctx, job.ID, []byte(part)); err != nil {
				t.Fatal(err)
			}
		}
		const whole = "hello world"
		cases := []struct {
			offset uint64
			max    int
			data   string
			next   uint64
		}{
			{0, 0, whole, 11}, {0, 5, "hello", 5}, {5, 3, " wo", 8}, {8, 0, "rld", 11}, {6, 100, "world", 11}, {11, 0, "", 11}, {11, 4, "", 11}, {0, 11, whole, 11}, {10, 1, "d", 11},
		}
		for _, c := range cases {
			chunk, err := e.s.ReadLog(e.ctx, job.ID, c.offset, c.max)
			if err != nil || string(chunk.Data) != c.data || chunk.Next != c.next {
				t.Errorf("ReadLog(offset %d, max %d) = %q next %d, %v; want %q next %d", c.offset, c.max, chunk.Data, chunk.Next, err, c.data, c.next)
			}
		}
		_, err := e.s.ReadLog(e.ctx, job.ID, 12, 0)
		wantKind(t, "an offset past the end", err, core.FailureInvalidInput)
		_, err = e.s.ReadLog(e.ctx, job.ID, 0, -1)
		wantKind(t, "a negative max", err, core.FailureInvalidInput)
	})

	t.Run("reading in pieces of any size gives back everything appended in any pieces", func(t *testing.T) {
		for seed := range uint64(10) {
			rng := rand.New(rand.NewPCG(seed, 9))
			e := newEnv(t, newStore)
			job := e.submit(e.command(1, "ls")).Job
			var want []byte
			for range 1 + rng.IntN(12) {
				piece := make([]byte, rng.IntN(300))
				for i := range piece {
					piece[i] = byte(rng.IntN(256))
				}
				if err := e.s.AppendLog(e.ctx, job.ID, piece); err != nil {
					t.Fatal(err)
				}
				want = append(want, piece...)
			}
			var got []byte
			next := uint64(0)
			step := 1 + rng.IntN(97)
			for next < uint64(len(want)) {
				chunk, err := e.s.ReadLog(e.ctx, job.ID, next, step)
				if err != nil || len(chunk.Data) == 0 || chunk.Next != next+uint64(len(chunk.Data)) {
					t.Fatalf("seed %d: ReadLog(%d, %d) = %d bytes next %d, %v", seed, next, step, len(chunk.Data), chunk.Next, err)
				}
				got, next = append(got, chunk.Data...), chunk.Next
			}
			if !bytes.Equal(got, want) {
				t.Fatalf("seed %d: read %d bytes, appended %d, and they differ", seed, len(got), len(want))
			}
		}
	})

	t.Run("fails with not found for a job that does not exist", func(t *testing.T) {
		e := newEnv(t, newStore)
		wantKind(t, "AppendLog", e.s.AppendLog(e.ctx, "j9", []byte("x")), core.FailureNotFound)
		_, err := e.s.ReadLog(e.ctx, "j9", 0, 0)
		wantKind(t, "ReadLog", err, core.FailureNotFound)
	})

	t.Run("logs are per job and the store keeps its own copy", func(t *testing.T) {
		e := newEnv(t, newStore)
		a, b := e.submit(e.command(1, "a")).Job, e.submit(e.command(2, "b")).Job
		data := []byte("aaa")
		if err := e.s.AppendLog(e.ctx, a.ID, data); err != nil {
			t.Fatal(err)
		}
		data[0] = 'X'
		chunk, err := e.s.ReadLog(e.ctx, a.ID, 0, 0)
		if err != nil || string(chunk.Data) != "aaa" {
			t.Fatalf("ReadLog = %q, %v; want the bytes as appended", chunk.Data, err)
		}
		chunk.Data[1] = 'Y'
		if again, _ := e.s.ReadLog(e.ctx, a.ID, 0, 0); string(again.Data) != "aaa" {
			t.Errorf("a caller changed the log: %q", again.Data)
		}
		if other, err := e.s.ReadLog(e.ctx, b.ID, 0, 0); err != nil || len(other.Data) != 0 {
			t.Errorf("the other job's log = %q, %v; want empty", other.Data, err)
		}
	})
}
