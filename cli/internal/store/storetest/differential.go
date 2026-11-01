package storetest

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// Differential drives the stores through the same random operations, the whole interface, with
// keys that repeat so submissions join and reuse, and with values some store may refuse. It fails
// at the first operation two stores answer differently, in the value or the kind of failure.
// Stores that pass the contract and agree here are interchangeable for a caller.
//
// After each step it calls between, which may replace a store in the slice, for instance with the
// same store reopened from disk: reopening must not change an answer.
func Differential(t *testing.T, seed uint64, steps int, stores []store.Store, between func(step int)) {
	t.Helper()
	d := &driver{t: t, g: newGen(seed), stores: stores, runs: []core.RunID{"run-a", "run-b"}}
	for step := range steps {
		d.step(step)
		if between != nil {
			between(step)
		}
	}
}

type driver struct {
	t      *testing.T
	g      *gen
	stores []store.Store
	runs   []core.RunID
}

type answer struct {
	value any
	kind  string
}

func kindOf(err error) string {
	var f *core.Failure
	switch {
	case err == nil:
		return ""
	case errors.As(err, &f):
		return string(f.Kind)
	case errors.Is(err, context.Canceled):
		return "canceled"
	}
	return "other: " + err.Error()
}

// do applies one operation to every store and checks that the answers agree.
func (d *driver) do(step int, name string, call func(store.Store) (any, error)) {
	d.t.Helper()
	var first answer
	for i, s := range d.stores {
		value, err := call(s)
		got := answer{value: value, kind: kindOf(err)}
		if i == 0 {
			first = got
			continue
		}
		if got.kind != first.kind || !reflect.DeepEqual(got.value, first.value) {
			d.t.Fatalf("step %d, %s: store %d answered %+v (%q), store 0 answered %+v (%q)", step, name, i, got.value, got.kind, first.value, first.kind)
		}
	}
}

func (d *driver) jobs() []core.Job {
	jobs, err := d.stores[0].Jobs(context.Background(), core.JobFilter{})
	if err != nil {
		d.t.Fatalf("Jobs: %v", err)
	}
	return jobs
}

func (d *driver) jobID(jobs []core.Job) core.JobID {
	if len(jobs) == 0 || d.g.pick(20) == 0 {
		return core.JobID(fmt.Sprintf("j%d", 1+d.g.pick(40)))
	}
	return jobs[d.g.pick(len(jobs))].ID
}

func (d *driver) planNumber() core.PlanNumber {
	plans, err := d.stores[0].Plans(context.Background(), core.PlanFilter{})
	if err != nil {
		d.t.Fatalf("Plans: %v", err)
	}
	if len(plans) == 0 || d.g.pick(15) == 0 {
		return core.PlanNumber(d.g.pick(30))
	}
	return plans[d.g.pick(len(plans))].Number
}

func (d *driver) instant() time.Time {
	at := d.g.instant()
	if d.g.pick(5) == 0 {
		return at.In(time.FixedZone("zone", 3600*(d.g.pick(20)-10)))
	}
	return at
}

func (d *driver) text() string {
	if d.g.pick(30) == 0 {
		return "bad\xff"
	}
	return d.g.text()
}

func (d *driver) step(step int) {
	g, ctx := d.g, context.Background()
	jobs := d.jobs()
	switch op := g.pick(17); {
	case op < 4:
		sub := core.Submission{Spec: g.spec(), Key: core.JobKey(fmt.Sprintf("k%d", g.pick(5))), NoCache: g.pick(6) == 0, At: d.instant()}
		sub.Spec.Commit = commit(g.pick(3))
		switch g.pick(3) {
		case 0:
			sub.Spec.Settings = core.CheckSettings{}
		case 1:
			sub.Spec.Settings = core.MeasureSettings{Samples: uint32(g.pick(3)), Recheck: g.pick(2) == 0}
		default:
			sub.Key = ""
		}
		if g.pick(2) == 0 {
			sub.Note = &core.Note{Text: d.text(), At: d.instant(), Agent: g.text()}
		}
		if g.pick(3) == 0 {
			sub.Plan = d.planNumber()
		}
		if g.pick(25) == 0 {
			sub.Spec.Settings = nil
		}
		d.do(step, "Submit", func(s store.Store) (any, error) { return s.Submit(ctx, sub) })
	case op < 8:
		d.transition(step, jobs)
	case op == 8:
		id, note := d.jobID(jobs), core.Note{Text: d.text(), At: d.instant(), Agent: g.text()}
		d.do(step, "AddNote", func(s store.Store) (any, error) { return s.AddNote(ctx, id, note) })
	case op == 9:
		p := g.promotion()
		if g.pick(10) == 0 {
			p.Commit = ""
		}
		d.do(step, "RecordPromotion", func(s store.Store) (any, error) { return nil, s.RecordPromotion(ctx, p) })
	case op == 10:
		np := core.NewPlan{Title: d.text(), Body: g.text(), At: d.instant()}
		d.do(step, "CreatePlan", func(s store.Store) (any, error) { return s.CreatePlan(ctx, np) })
	case op == 11:
		number, edit := d.planNumber(), core.PlanEdit{At: d.instant()}
		if g.pick(2) == 0 {
			edit.Title = ptr(d.text())
		}
		if g.pick(2) == 0 {
			edit.Body = ptr(g.text())
		}
		if g.pick(2) == 0 {
			edit.Status = ptr([]core.PlanStatus{core.PlanOpen, core.PlanChosen, core.PlanWon, core.PlanParked, core.PlanRejected}[g.pick(5)])
		}
		if g.pick(2) == 0 {
			edit.Comment = &core.PlanComment{Text: g.text(), At: d.instant(), Agent: g.text()}
		}
		d.do(step, "EditPlan", func(s store.Store) (any, error) { return s.EditPlan(ctx, number, edit) })
	case op == 12:
		run := d.runs[g.pick(len(d.runs))]
		if g.pick(25) == 0 {
			run = "bad run"
		}
		details := g.details()
		draft := g.draft(run, details[g.pick(len(details))])
		d.do(step, "AppendEvent", func(s store.Store) (any, error) { return s.AppendEvent(ctx, draft) })
	case op == 13:
		id, data := d.jobID(jobs), make([]byte, g.pick(200))
		for i := range data {
			data[i] = byte(g.pick(256))
		}
		d.do(step, "AppendLog", func(s store.Store) (any, error) { return nil, s.AppendLog(ctx, id, data) })
	default:
		d.read(step, jobs)
	}
}

func (d *driver) transition(step int, jobs []core.Job) {
	g, ctx := d.g, context.Background()
	id := d.jobID(jobs)
	tr := core.Transition{Job: id, From: core.JobQueued, To: core.JobRunning, At: d.instant()}
	for _, job := range jobs {
		if job.ID == id {
			tr = g.transitionFor(job, d.instant())
		}
	}
	d.do(step, "Transition", func(s store.Store) (any, error) { return s.Transition(ctx, tr) })
}

func (d *driver) read(step int, jobs []core.Job) {
	g, ctx := d.g, context.Background()
	switch g.pick(8) {
	case 0:
		id := d.jobID(jobs)
		d.do(step, "Job", func(s store.Store) (any, error) { return s.Job(ctx, id) })
	case 1:
		f := core.JobFilter{States: []core.JobState{core.JobStates()[g.pick(6)]}}
		if g.pick(2) == 0 {
			f.Commit = commit(g.pick(3))
		}
		if g.pick(3) == 0 {
			f.Kind = []core.JobKind{core.JobCheck, core.JobCommand, core.JobMeasure}[g.pick(3)]
		}
		if g.pick(2) == 0 {
			f.NoteContains = "e"
		}
		if g.pick(3) == 0 {
			f.States = nil
		}
		d.do(step, "Jobs", func(s store.Store) (any, error) { return s.Jobs(ctx, f) })
	case 2:
		f := core.ResultFilter{}
		if g.pick(2) == 0 {
			f.Commit = commit(g.pick(3))
		}
		if g.pick(2) == 0 {
			f.Key = core.JobKey(fmt.Sprintf("k%d", g.pick(5)))
		}
		d.do(step, "Results", func(s store.Store) (any, error) { return s.Results(ctx, f) })
	case 3:
		d.do(step, "Promotions", func(s store.Store) (any, error) { return s.Promotions(ctx) })
	case 4:
		n := d.planNumber()
		d.do(step, "Plan", func(s store.Store) (any, error) { return s.Plan(ctx, n) })
		f := core.PlanFilter{Text: []string{"", "e", "a"}[g.pick(3)]}
		if g.pick(2) == 0 {
			f.Statuses = []core.PlanStatus{core.PlanOpen, core.PlanChosen}
		}
		d.do(step, "Plans", func(s store.Store) (any, error) { return s.Plans(ctx, f) })
	case 5, 6:
		run, after, limit := d.runs[g.pick(len(d.runs))], uint64(g.pick(6)), g.pick(5)-1
		d.do(step, "Events", func(s store.Store) (any, error) { return s.Events(ctx, run, after, limit) })
	default:
		id, offset, limit := d.jobID(jobs), uint64(g.pick(300)), g.pick(120)-1
		if g.pick(2) == 0 {
			offset = 0
		}
		d.do(step, "ReadLog", func(s store.Store) (any, error) { return s.ReadLog(ctx, id, offset, limit) })
	}
}
