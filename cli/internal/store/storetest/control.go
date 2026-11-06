package storetest

import (
	"reflect"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func testControl(t *testing.T, newStore New) {
	t.Run("a hold of a queued job is kept with its note, and a release drops it", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.check(1, "key-a")).Job
		hold := core.JobHold{Agent: "lead", Reason: "after the integration check", At: e.now()}
		held, err := e.s.ControlJob(e.ctx, job.ID, core.HoldJob{Hold: hold})
		if err != nil {
			t.Fatal(err)
		}
		if held.Hold == nil || !reflect.DeepEqual(*held.Hold, hold) || len(held.Notes) != 1 || held.Notes[0].Agent != "lead" {
			t.Errorf("held job = %+v, want the hold and one note by its agent", held)
		}
		if stored := e.job(job.ID); !reflect.DeepEqual(stored, held) {
			t.Errorf("Job = %+v, want %+v", stored, held)
		}
		released, err := e.s.ControlJob(e.ctx, job.ID, core.ReleaseJob{Agent: "lead", At: e.now()})
		if err != nil {
			t.Fatal(err)
		}
		if released.Hold != nil || len(released.Notes) != 2 {
			t.Errorf("released job = %+v, want no hold and a second note", released)
		}
		again, err := e.s.ControlJob(e.ctx, job.ID, core.ReleaseJob{Agent: "lead", At: e.now()})
		if err != nil || !reflect.DeepEqual(again, released) {
			t.Errorf("a second release = %+v, %v; want the job unchanged", again, err)
		}
	})

	t.Run("only a queued job is held, and a job that starts is not held any more", func(t *testing.T) {
		for _, state := range core.JobStates() {
			e := newEnv(t, newStore)
			job := e.submit(e.check(1, "key-a")).Job
			e.reach(job, state)
			before := e.job(job.ID)
			_, err := e.s.ControlJob(e.ctx, job.ID, core.HoldJob{Hold: core.JobHold{At: e.now()}})
			if state == core.JobQueued {
				if err != nil {
					t.Fatalf("%s: %v", state, err)
				}
				e.reach(e.job(job.ID), core.JobRunning)
				if got := e.job(job.ID); got.Hold != nil {
					t.Errorf("a held job that started is still held: %+v", got.Hold)
				}
				continue
			}
			wantKind(t, "ControlJob of a "+string(state)+" job", err, core.FailureConflict)
			if got := e.job(job.ID); !reflect.DeepEqual(got, before) {
				t.Errorf("%s: a refused hold changed the job to %+v", state, got)
			}
		}
	})

	t.Run("an unknown job is not found and a hold with no time is invalid", func(t *testing.T) {
		e := newEnv(t, newStore)
		_, err := e.s.ControlJob(e.ctx, "j99", core.HoldJob{Hold: core.JobHold{At: e.now()}})
		wantKind(t, "ControlJob of an unknown job", err, core.FailureNotFound)
		job := e.submit(e.check(1, "key-a")).Job
		_, err = e.s.ControlJob(e.ctx, job.ID, core.HoldJob{})
		wantKind(t, "a hold with no time", err, core.FailureInvalidInput)
		_, err = e.s.ControlJob(e.ctx, job.ID, nil)
		wantKind(t, "a nil control", err, core.FailureInvalidInput)
		if got := e.job(job.ID); got.Hold != nil || len(got.Notes) != 0 {
			t.Errorf("a refused change changed the job: %+v", got)
		}
	})

	t.Run("a queued job's priority is set with its note, needs a reason and is kept", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.check(1, "key-a")).Job
		at := e.now()
		high, err := e.s.ControlJob(e.ctx, job.ID, core.SetJobPriority{Priority: core.PriorityHigh, Reason: "gates the others", Agent: "lead", At: at})
		if err != nil {
			t.Fatal(err)
		}
		want := core.Note{Text: core.PriorityChange("", core.PriorityHigh, "gates the others"), At: at, Agent: "lead"}
		if high.Priority != core.PriorityHigh || len(high.Notes) != 1 || !reflect.DeepEqual(high.Notes[0], want) {
			t.Errorf("job = %+v, want high with the note %+v", high, want)
		}
		if stored := e.job(job.ID); !reflect.DeepEqual(stored, high) {
			t.Errorf("Job = %+v, want %+v", stored, high)
		}
		for _, bad := range []core.SetJobPriority{
			{Priority: core.PriorityLow, At: e.now()},
			{Priority: "urgent", Reason: "r", At: e.now()},
			{Priority: core.PriorityLow, Reason: "r"},
		} {
			_, err := e.s.ControlJob(e.ctx, job.ID, bad)
			wantKind(t, "a bad priority change", err, core.FailureInvalidInput)
		}
		inherit, err := e.s.ControlJob(e.ctx, job.ID, core.SetJobPriority{Reason: "back to the plan's", At: e.now()})
		if err != nil || inherit.Priority != "" || len(inherit.Notes) != 2 {
			t.Errorf("inherit = %+v, %v", inherit, err)
		}
		e.reach(e.job(job.ID), core.JobRunning)
		_, err = e.s.ControlJob(e.ctx, job.ID, core.SetJobPriority{Priority: core.PriorityHigh, Reason: "r", At: e.now()})
		wantKind(t, "the priority of a running job", err, core.FailureConflict)
	})

	t.Run("a plan's priority is edited with its comment as one change", func(t *testing.T) {
		e := newEnv(t, newStore)
		plan, err := e.s.CreatePlan(e.ctx, core.NewPlan{Title: "t", At: e.now()})
		if err != nil {
			t.Fatal(err)
		}
		if plan.Priority != "" {
			t.Errorf("a new plan has priority %q, want none (normal)", plan.Priority)
		}
		high := core.PriorityHigh
		comment := core.PlanComment{Text: core.PriorityChange(core.PriorityNormal, high, "gates the others"), At: e.now(), Agent: "lead"}
		edited, err := e.s.EditPlan(e.ctx, plan.Number, core.PlanEdit{Priority: &high, Comment: &comment, At: e.now()})
		if err != nil || edited.Priority != high || len(edited.Comments) != 1 {
			t.Fatalf("EditPlan = %+v, %v", edited, err)
		}
		if got, err := e.s.Plan(e.ctx, plan.Number); err != nil || !reflect.DeepEqual(got, edited) {
			t.Errorf("Plan = %+v, %v; want %+v", got, err, edited)
		}
		bad := core.Priority("urgent")
		_, err = e.s.EditPlan(e.ctx, plan.Number, core.PlanEdit{Priority: &bad, At: e.now()})
		wantKind(t, "a plan priority that is none", err, core.FailureInvalidInput)
	})
}
