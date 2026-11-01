package storetest

import (
	"math"
	"reflect"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func testValues(t *testing.T, newStore New) {
	t.Run("the generators set every field", func(t *testing.T) {
		g := newGen(1)
		for range 20 {
			populated(t, "job", g.job())
			populated(t, "plan", g.plan())
			populated(t, "promotion", g.promotion())
			populated(t, "refusal", g.refusal())
			populated(t, "note", g.note())
			populated(t, "measure result", g.result("j1", "k", "c", true))
			populated(t, "check result", g.result("j1", "k", "c", false))
			populated(t, "draft", g.draft("run", g.job()))
		}
	})

	t.Run("a job, its result, a plan, a promotion and an event come back as they went in", func(t *testing.T) {
		for seed := range uint64(25) {
			g := newGen(seed)
			e := newEnv(t, newStore)
			measure := g.pick(2) == 0
			sub := core.Submission{Spec: g.spec(), Key: core.JobKey(g.name()), Note: ptr(g.note()), At: g.instant()}
			sub.Spec.Settings = core.CheckSettings{}
			if measure {
				sub.Spec.Settings = core.MeasureSettings{Samples: 1 + g.r.Uint32(), Recheck: g.pick(2) == 0}
			}
			plan := e.createPlan("link")
			sub.Plan = plan.Number
			job := e.submit(sub).Job
			command := core.Submission{Spec: g.spec(), Note: ptr(g.note()), At: g.instant()}
			command.Spec.Settings = core.CommandSettings{Argv: g.strs(), Service: &core.ServiceSpec{Argv: g.strs(), Probe: g.strs()}, Timeout: time.Duration(1 + g.r.Int64N(1<<40))}
			if got := e.submit(command).Job; !reflect.DeepEqual(got.Spec, command.Spec) || got.Key != "" {
				t.Fatalf("seed %d: the submitted command job = %+v, want what was asked for in %+v", seed, got, command)
			}
			if !reflect.DeepEqual(job.Spec, sub.Spec) || !job.SubmittedAt.Equal(sub.At) || job.Notes[0] != *sub.Note {
				t.Fatalf("seed %d: the submitted job = %+v, want what was asked for in %+v", seed, job, sub)
			}

			env := g.pinned()
			code := int(g.r.Int32())
			startedAt := g.instant()
			running, err := e.s.Transition(e.ctx, core.Transition{Job: job.ID, From: core.JobQueued, To: core.JobRunning, At: startedAt, Environment: &env})
			if err != nil {
				t.Fatalf("seed %d: %v", seed, err)
			}
			res := g.acceptedResult(running, measure)
			finish := core.Transition{Job: job.ID, From: core.JobRunning, To: core.JobSucceeded, At: g.instant(), ExitCode: &code, Result: &res}
			if measure {
				finish.CheckJob = res.Outcome.(core.MeasureOutcome).CheckJob
			}
			done, err := e.s.Transition(e.ctx, finish)
			if err != nil {
				t.Fatalf("seed %d: %v", seed, err)
			}
			if got := e.job(job.ID); !reflect.DeepEqual(got, done) || done.Environment == nil || !reflect.DeepEqual(*done.Environment, env) {
				t.Errorf("seed %d: Job = %+v, want %+v with environment %+v", seed, got, done, env)
			}
			if got := e.results(core.ResultFilter{}); len(got) != 1 || !reflect.DeepEqual(got[0], res) {
				t.Errorf("seed %d: Results = %+v, want %+v", seed, got, res)
			}

			np := core.NewPlan{Title: g.text(), Body: g.text(), At: g.instant()}
			created, err := e.s.CreatePlan(e.ctx, np)
			if err != nil || created.Title != np.Title || created.Body != np.Body || !created.CreatedAt.Equal(np.At) {
				t.Errorf("seed %d: CreatePlan = %+v, %v; want %+v", seed, created, err, np)
			}
			title, body, status, comment := g.text(), g.text(), core.PlanRejected, g.comment()
			edited, err := e.s.EditPlan(e.ctx, created.Number, core.PlanEdit{Title: &title, Body: &body, Status: &status, Comment: &comment, At: g.instant()})
			if err != nil {
				t.Fatalf("seed %d: %v", seed, err)
			}
			if got, err := e.s.Plan(e.ctx, created.Number); err != nil || !reflect.DeepEqual(got, edited) || edited.Title != title || edited.Body != body || edited.Comments[0] != comment {
				t.Errorf("seed %d: Plan = %+v, %v; want %+v", seed, got, err, edited)
			}

			p := g.promotion()
			if err := e.s.RecordPromotion(e.ctx, p); err != nil {
				t.Fatalf("seed %d: %v", seed, err)
			}
			if got, err := e.s.Promotions(e.ctx); err != nil || len(got) != 1 || !reflect.DeepEqual(got[0], p) {
				t.Errorf("seed %d: Promotions = %+v, %v; want %+v", seed, got, err, p)
			}

			for i, detail := range g.details() {
				d := g.draft("run-a", detail)
				ev, err := e.s.AppendEvent(e.ctx, d)
				if err != nil || ev.Sequence != uint64(i+1) || !reflect.DeepEqual(ev.EventDraft, d) {
					t.Fatalf("seed %d: AppendEvent(%T) = %+v, %v; want position %d and the draft kept", seed, detail, ev, err, i+1)
				}
				if got, err := e.s.Events(e.ctx, "run-a", uint64(i), 1); err != nil || len(got) != 1 || !reflect.DeepEqual(got[0], ev) {
					t.Fatalf("seed %d: Events after %d = %+v, %v; want %+v", seed, i, got, err, ev)
				}
			}
		}
	})

	t.Run("values with nothing set come back with nothing set", func(t *testing.T) {
		e := newEnv(t, newStore)
		sub := core.Submission{Spec: core.JobSpec{Commit: "c", Settings: core.CommandSettings{}}}
		job := e.submit(sub).Job
		if got := e.job(job.ID); !reflect.DeepEqual(got, job) || !got.SubmittedAt.IsZero() || got.Notes != nil || got.Plans != nil {
			t.Errorf("Job = %+v, want the bare job back", got)
		}
		fp := pinned()
		e.move(job.ID, core.JobQueued, core.JobRunning)
		res := core.Result{Job: job.ID, Outcome: core.CheckOutcome{}, Commit: "c", SchemaVersion: core.SchemaVersion, Environment: fp}
		if _, err := e.s.Transition(e.ctx, core.Transition{Job: job.ID, From: core.JobRunning, To: core.JobSucceeded, At: e.now(), Result: &res}); err == nil {
			t.Errorf("a command job saved a result")
		}
		other := e.submit(core.Submission{Spec: core.JobSpec{Commit: "c", Settings: core.CheckSettings{}}, Key: "k"}).Job
		e.move(other.ID, core.JobQueued, core.JobRunning)
		bare := core.Result{Job: other.ID, Key: "k", Commit: "c", SchemaVersion: core.SchemaVersion, Environment: fp, Outcome: core.CheckOutcome{}}
		if _, err := e.s.Transition(e.ctx, core.Transition{Job: other.ID, From: core.JobRunning, To: core.JobSucceeded, At: e.now(), Result: &bare}); err != nil {
			t.Fatal(err)
		}
		if got := e.results(core.ResultFilter{}); len(got) != 1 || !reflect.DeepEqual(got[0], bare) {
			t.Errorf("Results = %+v, want the bare result %+v", got, bare)
		}
		ev, err := e.s.AppendEvent(e.ctx, core.EventDraft{Run: "r"})
		if err != nil || ev.Sequence != 1 || ev.Detail != nil {
			t.Errorf("AppendEvent of a bare draft = %+v, %v", ev, err)
		}
	})

	t.Run("an empty slice or map comes back nil", func(t *testing.T) {
		e := newEnv(t, newStore)
		sub := e.command(1)
		sub.Spec.Settings = core.CommandSettings{Argv: []string{}, Service: &core.ServiceSpec{Argv: []string{}, Probe: []string{}}}
		job := e.submit(sub).Job
		cs := job.Spec.Settings.(core.CommandSettings)
		if cs.Argv != nil || cs.Service.Argv != nil || cs.Service.Probe != nil {
			t.Errorf("settings = %+v, want nil for each empty slice", cs)
		}
		got := e.job(job.ID).Spec.Settings.(core.CommandSettings)
		if !reflect.DeepEqual(got, cs) {
			t.Errorf("Job settings = %+v, want %+v", got, cs)
		}
	})

	t.Run("a time comes back in UTC, to the nanosecond", func(t *testing.T) {
		e := newEnv(t, newStore)
		zone := time.FixedZone("UTC+5:30", 5*3600+1800)
		in := time.Date(2026, 11, 1, 12, 30, 45, 123456789, zone)
		sub := e.check(1, "key-a")
		sub.At, sub.Note = in, &core.Note{Text: "n", At: in.Add(time.Hour)}
		job := e.submit(sub).Job
		for _, got := range []time.Time{job.SubmittedAt, e.job(job.ID).SubmittedAt, e.job(job.ID).Notes[0].At} {
			if !got.Equal(in) && !got.Equal(in.Add(time.Hour)) {
				t.Errorf("time = %v, want the instant %v", got, in)
			}
			if got.Location() != time.UTC || got.Nanosecond() != in.Nanosecond() {
				t.Errorf("time = %v in %v with %d ns, want UTC with %d ns", got, got.Location(), got.Nanosecond(), in.Nanosecond())
			}
		}
		if !reflect.DeepEqual(e.job(job.ID).SubmittedAt, in.UTC()) {
			t.Errorf("stored time = %#v, want %#v", e.job(job.ID).SubmittedAt, in.UTC())
		}
	})

	t.Run("refuses a value it cannot hold, whole, and changes nothing", func(t *testing.T) {
		e := newEnv(t, newStore)
		g := newGen(7)
		job := e.submit(e.measure(1, "key-a", 3)).Job
		e.move(job.ID, core.JobQueued, core.JobRunning)
		runs := []core.RunID{"run-a"}
		if _, err := e.s.AppendEvent(e.ctx, g.draft("run-a", nil)); err != nil {
			t.Fatal(err)
		}
		before := snapshot(t, e.s, runs...)
		finish := func(mutate func(*core.Result)) error {
			res := e.resultFor(job, 1, 2, 3)
			mutate(&res)
			_, err := e.s.Transition(e.ctx, core.Transition{Job: job.ID, From: core.JobRunning, To: core.JobSucceeded, At: e.now(), Result: &res})
			return err
		}
		sample := func(f float64) func(*core.Result) {
			return func(r *core.Result) {
				r.Outcome = core.MeasureOutcome{Metric: core.Metric{Name: "ops", Direction: core.Maximize}, Samples: []float64{1, f, 3}, CheckJob: "j-check"}
			}
		}
		for _, c := range []struct {
			name   string
			mutate func(*core.Result)
		}{
			{"a NaN sample", sample(math.NaN())},
			{"an infinite sample", sample(math.Inf(1))},
			{"a negative infinite sample", sample(math.Inf(-1))},
			{"text that is not UTF-8 in a fact", func(r *core.Result) { r.Environment.Facts = map[string]string{"k": "v\xff"} }},
			{"a fact name that is not UTF-8", func(r *core.Result) { r.Environment.Facts = map[string]string{"k\xfe": "v"} }},
			{"a load that is not finite", func(r *core.Result) { r.Load.CPUBusyFraction = math.NaN() }},
			{"a time before year 0", func(r *core.Result) { r.RecordedAt = time.Date(-1, 1, 1, 0, 0, 0, 0, time.UTC) }},
			{"a time after year 9999", func(r *core.Result) { r.RecordedAt = time.Date(10000, 1, 1, 0, 0, 0, 0, time.UTC) }},
			{"an outcome held by pointer", func(r *core.Result) { r.Outcome = &core.CheckOutcome{} }},
		} {
			wantKind(t, c.name, finish(c.mutate), core.FailureInvalidInput)
		}
		draft := g.draft("run-a", nil)
		draft.Detail = &core.Plan{Title: "by pointer"}
		_, err := e.s.AppendEvent(e.ctx, draft)
		wantKind(t, "an event detail held by pointer", err, core.FailureInvalidInput)
		draft.Detail = core.Failure{Message: "bad\xff"}
		_, err = e.s.AppendEvent(e.ctx, draft)
		wantKind(t, "an event detail with text that is not UTF-8", err, core.FailureInvalidInput)
		wantKind(t, "a promotion with a NaN verdict noise", e.s.RecordPromotion(e.ctx, func() core.Promotion {
			p := g.promotion()
			p.Verdict.Noise = math.NaN()
			return p
		}()), core.FailureInvalidInput)
		unchanged(t, "a refused value", e.s, before, runs...)
	})
}

func ptr[T any](v T) *T { return &v }

func testFailures(t *testing.T, newStore New) {
	t.Run("a failure is a *core.Failure of the documented kind with a message", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.check(1, "key-a")).Job
		_, notFound := e.s.Job(e.ctx, "j404")
		_, conflict := e.s.Transition(e.ctx, core.Transition{Job: job.ID, From: core.JobRunning, To: core.JobFailed, At: e.now()})
		_, invalid := e.s.Submit(e.ctx, core.Submission{})
		wantKind(t, "an unknown job", notFound, core.FailureNotFound)
		wantKind(t, "a stale transition", conflict, core.FailureConflict)
		wantKind(t, "an empty submission", invalid, core.FailureInvalidInput)
	})
}

func testCancelled(t *testing.T, newStore New) {
	t.Run("every method fails with the context's error and changes nothing", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.command(1, "ls")).Job
		plan := e.createPlan("idea")
		if err := e.s.AppendLog(e.ctx, job.ID, []byte("out")); err != nil {
			t.Fatal(err)
		}
		if _, err := e.s.AppendEvent(e.ctx, newGen(1).draft("run-a", nil)); err != nil {
			t.Fatal(err)
		}
		before := snapshot(t, e.s, "run-a")
		ctx, cancel := cancelledContext()
		defer cancel()
		g := newGen(3)
		title := "x"
		calls := map[string]func() error{
			"Submit": func() error { _, err := e.s.Submit(ctx, e.check(2, "key-a")); return err },
			"Job":    func() error { _, err := e.s.Job(ctx, job.ID); return err },
			"Jobs":   func() error { _, err := e.s.Jobs(ctx, core.JobFilter{}); return err },
			"Transition": func() error {
				_, err := e.s.Transition(ctx, core.Transition{Job: job.ID, From: core.JobQueued, To: core.JobRunning, At: e.now()})
				return err
			},
			"AddNote":         func() error { _, err := e.s.AddNote(ctx, job.ID, *e.note("n")); return err },
			"Results":         func() error { _, err := e.s.Results(ctx, core.ResultFilter{}); return err },
			"RecordPromotion": func() error { return e.s.RecordPromotion(ctx, g.promotion()) },
			"Promotions":      func() error { _, err := e.s.Promotions(ctx); return err },
			"CreatePlan":      func() error { _, err := e.s.CreatePlan(ctx, core.NewPlan{Title: "t", At: e.now()}); return err },
			"Plan":            func() error { _, err := e.s.Plan(ctx, plan.Number); return err },
			"Plans":           func() error { _, err := e.s.Plans(ctx, core.PlanFilter{}); return err },
			"EditPlan": func() error {
				_, err := e.s.EditPlan(ctx, plan.Number, core.PlanEdit{Title: &title, At: e.now()})
				return err
			},
			"AppendEvent": func() error { _, err := e.s.AppendEvent(ctx, g.draft("run-a", nil)); return err },
			"Events":      func() error { _, err := e.s.Events(ctx, "run-a", 0, 0); return err },
			"AppendLog":   func() error { return e.s.AppendLog(ctx, job.ID, []byte("more")) },
			"ReadLog":     func() error { _, err := e.s.ReadLog(ctx, job.ID, 0, 0); return err },
		}
		for name, call := range calls {
			if err := call(); !isCancelled(err) {
				t.Errorf("%s with a cancelled context = %v, want the context's error", name, err)
			}
		}
		unchanged(t, "calls with a cancelled context", e.s, before, "run-a")
	})
}
