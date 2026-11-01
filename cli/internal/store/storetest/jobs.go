package storetest

import (
	"errors"
	"fmt"
	"math/rand/v2"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func testTransition(t *testing.T, newStore New) {
	t.Run("moves a job and records when", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.measure(1, "key-a", 3)).Job
		fp := core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"os": "linux"}}
		startAt := e.now()
		running, err := e.s.Transition(e.ctx, core.Transition{Job: job.ID, From: core.JobQueued, To: core.JobRunning, At: startAt, Environment: &fp})
		if err != nil {
			t.Fatal(err)
		}
		if running.State != core.JobRunning || running.StartedAt == nil || !running.StartedAt.Equal(startAt) || running.FinishedAt != nil {
			t.Errorf("running job = %+v, want running, started at %v, not finished", running, startAt)
		}
		if running.Environment == nil || !reflect.DeepEqual(*running.Environment, fp) {
			t.Errorf("environment = %+v, want %+v", running.Environment, fp)
		}
		endAt := e.now()
		code, check := 0, core.JobID("j-check")
		done, err := e.s.Transition(e.ctx, core.Transition{Job: job.ID, From: core.JobRunning, To: core.JobFailed, At: endAt, ExitCode: &code, CheckJob: check})
		if err != nil {
			t.Fatal(err)
		}
		if done.State != core.JobFailed || done.FinishedAt == nil || !done.FinishedAt.Equal(endAt) || done.ExitCode == nil || *done.ExitCode != 0 || done.CheckJob != check {
			t.Errorf("failed job = %+v, want failed at %v with exit 0 and check %s", done, endAt, check)
		}
		if !done.StartedAt.Equal(startAt) || done.Environment == nil {
			t.Errorf("a later move lost what the earlier one recorded: %+v", done)
		}
		if got := e.job(job.ID); !reflect.DeepEqual(got, done) {
			t.Errorf("Job = %+v, want %+v", got, done)
		}
	})

	t.Run("a job cancelled while queued never started", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.check(1, "key-a")).Job
		out, err := e.s.Transition(e.ctx, core.Transition{Job: job.ID, From: core.JobQueued, To: core.JobCancelled, At: e.now(), CancelReason: "not needed"})
		if err != nil {
			t.Fatal(err)
		}
		if out.State != core.JobCancelled || out.CancelReason != "not needed" || out.StartedAt != nil || out.FinishedAt == nil {
			t.Errorf("job = %+v, want cancelled with a reason, never started, finished", out)
		}
	})

	t.Run("saves a result together with the state it belongs to", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.measure(1, "key-a", 3)).Job
		res := e.resultFor(job, 10, 11, 12)
		e.move(job.ID, core.JobQueued, core.JobRunning)
		if n := len(e.results(core.ResultFilter{})); n != 0 {
			t.Fatalf("%d results before the job succeeded", n)
		}
		done, err := e.s.Transition(e.ctx, core.Transition{Job: job.ID, From: core.JobRunning, To: core.JobSucceeded, At: e.now(), Result: &res})
		if err != nil {
			t.Fatal(err)
		}
		if done.State != core.JobSucceeded || done.ResultJob != job.ID {
			t.Errorf("job = %+v, want succeeded with its own result", done)
		}
		got := e.results(core.ResultFilter{})
		if len(got) != 1 || !reflect.DeepEqual(got[0], res) {
			t.Errorf("results = %+v, want %+v", got, res)
		}
	})

	t.Run("fails with a conflict unless the job is in the state it moves from", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.check(1, "key-a")).Job
		stale := core.Transition{Job: job.ID, From: core.JobRunning, To: core.JobSucceeded, At: e.now()}
		before := snapshot(t, e.s)
		_, err := e.s.Transition(e.ctx, stale)
		wantKind(t, "a job that is still queued", err, core.FailureConflict)
		unchanged(t, "a conflicting transition", e.s, before)
		e.move(job.ID, core.JobQueued, core.JobRunning)
		repeat := core.Transition{Job: job.ID, From: core.JobQueued, To: core.JobRunning, At: e.now()}
		_, err = e.s.Transition(e.ctx, repeat)
		wantKind(t, "the same move twice", err, core.FailureConflict)
		if got := e.job(job.ID); got.State != core.JobRunning {
			t.Errorf("state = %s, want running", got.State)
		}
	})

	t.Run("fails with not found for a job that does not exist", func(t *testing.T) {
		e := newEnv(t, newStore)
		_, err := e.s.Transition(e.ctx, core.Transition{Job: "j99", From: core.JobQueued, To: core.JobRunning, At: e.now()})
		wantKind(t, "Transition", err, core.FailureNotFound)
		_, err = e.s.Job(e.ctx, "j99")
		wantKind(t, "Job", err, core.FailureNotFound)
		_, err = e.s.AddNote(e.ctx, "j99", *e.note("x"))
		wantKind(t, "AddNote", err, core.FailureNotFound)
	})

	t.Run("saves no result for a job that did not succeed, and none core would not accept", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.measure(1, "key-a", 3)).Job
		cmd := e.submit(e.command(2, "ls")).Job
		e.move(job.ID, core.JobQueued, core.JobRunning)
		e.move(cmd.ID, core.JobQueued, core.JobRunning)
		good := e.resultFor(job, 1, 2, 3)
		wrongJob, wrongKey, wrongCommit, noOutcome, forCommand := good, good, good, good, e.resultFor(job, 1)
		wrongJob.Job = "j77"
		wrongKey.Key = "key-z"
		wrongCommit.Commit = commit(9)
		noOutcome.Outcome = nil
		forCommand.Job, forCommand.Key = cmd.ID, ""
		oldSchema, otherEnv, noSamples, noCheck, checkOutcome := good, good, good, good, good
		oldSchema.SchemaVersion = core.SchemaVersion + 1
		otherEnv.Environment = core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCUDA}
		noSamples.Outcome = core.MeasureOutcome{Metric: core.Metric{Name: "ops", Direction: core.Maximize}, CheckJob: "j-check"}
		noCheck.Outcome = core.MeasureOutcome{Metric: core.Metric{Name: "ops", Direction: core.Maximize}, Samples: []float64{1}}
		checkOutcome.Outcome = core.CheckOutcome{}
		before := snapshot(t, e.s)
		cases := []struct {
			name string
			id   core.JobID
			to   core.JobState
			res  core.Result
		}{
			{"a failed job", job.ID, core.JobFailed, good},
			{"a cancelled job", job.ID, core.JobCancelled, good},
			{"a lost job", job.ID, core.JobLost, good},
			{"a result of another job", job.ID, core.JobSucceeded, wrongJob},
			{"a result under another key", job.ID, core.JobSucceeded, wrongKey},
			{"a result for another commit", job.ID, core.JobSucceeded, wrongCommit},
			{"a result with no outcome", job.ID, core.JobSucceeded, noOutcome},
			{"a result of an older schema", job.ID, core.JobSucceeded, oldSchema},
			{"a result taken in another environment than the job pinned", job.ID, core.JobSucceeded, otherEnv},
			{"a measurement with no samples", job.ID, core.JobSucceeded, noSamples},
			{"a measurement that names no check pass", job.ID, core.JobSucceeded, noCheck},
			{"a check outcome for a measure job", job.ID, core.JobSucceeded, checkOutcome},
			{"a result of a command job", cmd.ID, core.JobSucceeded, forCommand},
		}
		for _, c := range cases {
			res := c.res
			_, err := e.s.Transition(e.ctx, core.Transition{Job: c.id, From: core.JobRunning, To: c.to, At: e.now(), Result: &res})
			wantKind(t, c.name, err, core.FailureInvalidInput)
		}
		unchanged(t, "a refused result", e.s, before)
	})

	t.Run("refuses a state that is not a job state", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.check(1, "key-a")).Job
		before := snapshot(t, e.s)
		_, err := e.s.Transition(e.ctx, core.Transition{Job: job.ID, From: core.JobQueued, To: "paused", At: e.now()})
		wantKind(t, "a move to paused", err, core.FailureInvalidInput)
		_, err = e.s.Transition(e.ctx, core.Transition{Job: job.ID, From: "", To: core.JobCancelled, At: e.now()})
		wantKind(t, "a move from nothing: the job is not in it", err, core.FailureConflict)
		unchanged(t, "a refused move", e.s, before)
	})

	t.Run("never replaces a saved result", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.savedJob(e.check(1, "key-a"))
		before := snapshot(t, e.s)
		again := e.resultFor(job)
		again.Task = "another"
		_, err := e.s.Transition(e.ctx, core.Transition{Job: job.ID, From: core.JobSucceeded, To: core.JobSucceeded, At: e.now(), Result: &again})
		wantKind(t, "a second result: a finished job moves nowhere", err, core.FailureInvalidInput)
		unchanged(t, "a second result", e.s, before)
	})

	t.Run("answers every transition as core.Job.Apply does", func(t *testing.T) {
		applied, refused := 0, map[core.FailureKind]int{}
		for seed := range uint64(10) {
			g := newGen(seed)
			e := newEnv(t, newStore)
			var ids []core.JobID
			for i := range 20 {
				key := fmt.Sprintf("key-%d", i)
				var sub core.Submission
				switch i % 4 {
				case 0:
					sub = e.check(i%3+1, key)
				case 1, 2:
					sub = e.measure(i%3+1, key, 3)
				default:
					sub = e.command(i%3+1, "ls")
				}
				ids = append(ids, e.submit(sub).Job.ID)
			}
			for range 100 {
				job := e.job(ids[g.pick(len(ids))])
				for tries := 0; job.State.Final() && tries < 3; tries++ { // most attempts are on jobs that can still move
					job = e.job(ids[g.pick(len(ids))])
				}
				tr := g.transitionFor(job, e.now())
				want, wantErr := job.Apply(tr)
				before := snapshot(t, e.s)
				got, err := e.s.Transition(e.ctx, tr)
				if wantErr != nil {
					var f *core.Failure
					errors.As(wantErr, &f)
					wantKind(t, fmt.Sprintf("seed %d: %s %s->%s, which core refuses with %q", seed, job.ID, tr.From, tr.To, wantErr), err, f.Kind)
					unchanged(t, "a transition core refuses", e.s, before)
					refused[f.Kind]++
					continue
				}
				applied++
				if err != nil || !reflect.DeepEqual(got, want) {
					t.Fatalf("seed %d: %s %s->%s = %+v, %v; core applies it and gives %+v", seed, job.ID, tr.From, tr.To, got, err, want)
				}
				if !reflect.DeepEqual(e.job(job.ID), want) {
					t.Fatalf("seed %d: Job(%s) = %+v, want %+v", seed, job.ID, e.job(job.ID), want)
				}
				wantSaved := len(before.Results)
				if tr.Result != nil {
					wantSaved++
				}
				if saved := len(e.results(core.ResultFilter{})); saved != wantSaved {
					t.Fatalf("seed %d: %d results saved after the move of %s, want %d", seed, saved, job.ID, wantSaved)
				}
			}
		}
		if applied < 100 || refused[core.FailureConflict] < 50 || refused[core.FailureInvalidInput] < 50 {
			t.Errorf("%d transitions applied, %d refused as conflicts and %d as invalid input; the test would not tell the rules apart",
				applied, refused[core.FailureConflict], refused[core.FailureInvalidInput])
		}
	})
}

func testNotes(t *testing.T, newStore New) {
	t.Run("appends to a job in any state, in order", func(t *testing.T) {
		for _, state := range core.JobStates() {
			e := newEnv(t, newStore)
			job := e.submit(e.check(1, "key-a")).Job
			e.reach(job, state)
			one, two := *e.note("one"), *e.note("two")
			if _, err := e.s.AddNote(e.ctx, job.ID, one); err != nil {
				t.Fatalf("%s: %v", state, err)
			}
			got, err := e.s.AddNote(e.ctx, job.ID, two)
			if err != nil {
				t.Fatalf("%s: %v", state, err)
			}
			if !reflect.DeepEqual(got.Notes, []core.Note{one, two}) {
				t.Errorf("%s: notes = %+v, want both in order", state, got.Notes)
			}
			if stored := e.job(job.ID); !reflect.DeepEqual(stored, got) {
				t.Errorf("%s: Job = %+v, want %+v", state, stored, got)
			}
		}
	})

	t.Run("never changes the key or the state", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.measure(1, "key-a", 3)).Job
		got, err := e.s.AddNote(e.ctx, job.ID, *e.note("hello"))
		if err != nil {
			t.Fatal(err)
		}
		got.Notes = nil
		if !reflect.DeepEqual(got, job) {
			t.Errorf("job after a note = %+v, want the job as it was, %+v", got, job)
		}
		if out := e.submit(e.measure(1, "key-a", 3)); out.Job.ID != job.ID {
			t.Errorf("a submission after a note = %s, want to join %s: a note is not part of the key", out.Job.ID, job.ID)
		}
	})
}

func testJobs(t *testing.T, newStore New) {
	t.Run("an empty store lists nothing", func(t *testing.T) {
		e := newEnv(t, newStore)
		if got := e.jobs(core.JobFilter{}); len(got) != 0 {
			t.Errorf("Jobs = %+v, want none", got)
		}
	})

	t.Run("filters by commit, state, kind and note text, all together", func(t *testing.T) {
		for seed := range uint64(10) {
			rng := rand.New(rand.NewPCG(seed, 3))
			e := newEnv(t, newStore)
			notes := []string{"fast path", "cache miss", "Fast", "retry"}
			for i := range 14 {
				var sub core.Submission
				switch rng.IntN(3) {
				case 0:
					sub = e.check(rng.IntN(3)+1, "k"+string(rune('a'+i)))
				case 1:
					sub = e.measure(rng.IntN(3)+1, "k"+string(rune('a'+i)), 3)
				default:
					sub = e.command(rng.IntN(3)+1, "ls")
				}
				if rng.IntN(2) == 0 {
					sub.Note = e.note(notes[rng.IntN(len(notes))])
				}
				job := e.submit(sub).Job
				if n := rng.IntN(3); n > 0 {
					e.move(job.ID, core.JobQueued, core.JobRunning)
					if n == 2 {
						e.move(job.ID, core.JobRunning, core.JobFailed)
					}
				}
				if rng.IntN(4) == 0 {
					if _, err := e.s.AddNote(e.ctx, job.ID, *e.note(notes[rng.IntN(len(notes))])); err != nil {
						t.Fatal(err)
					}
				}
			}
			all := e.jobs(core.JobFilter{})
			if len(all) != 14 {
				t.Fatalf("seed %d: %d jobs, want 14", seed, len(all))
			}
			for range 40 {
				f := core.JobFilter{}
				if rng.IntN(2) == 0 {
					f.Commit = commit(rng.IntN(4) + 1)
				}
				if rng.IntN(2) == 0 {
					f.States = []core.JobState{core.JobStates()[rng.IntN(6)], core.JobStates()[rng.IntN(6)]}
				}
				if rng.IntN(3) == 0 {
					f.Kind = []core.JobKind{core.JobCheck, core.JobCommand, core.JobMeasure}[rng.IntN(3)]
				}
				if rng.IntN(3) == 0 {
					f.NoteContains = []string{"fast", "Fast", "retry", "miss", "zzz"}[rng.IntN(5)]
				}
				var want []core.Job
				for _, job := range all {
					if jobMatches(f, job) {
						want = append(want, job)
					}
				}
				if got := e.jobs(f); !reflect.DeepEqual(got, want) {
					t.Fatalf("seed %d: Jobs(%+v) = %d jobs, want %d: %+v", seed, f, len(got), len(want), want)
				}
			}
		}
	})
}

// jobMatches is the filter written out again, from the documentation of core.JobFilter.
func jobMatches(f core.JobFilter, job core.Job) bool {
	if f.Commit != "" && job.Spec.Commit != f.Commit {
		return false
	}
	if len(f.States) > 0 && !slices.Contains(f.States, job.State) {
		return false
	}
	if f.Kind != "" && job.Spec.Settings.Kind() != f.Kind {
		return false
	}
	if f.NoteContains != "" {
		found := false
		for _, n := range job.Notes {
			found = found || strings.Contains(n.Text, f.NoteContains)
		}
		return found
	}
	return true
}

func testResults(t *testing.T, newStore New) {
	t.Run("an empty store has no results and no promotions", func(t *testing.T) {
		e := newEnv(t, newStore)
		if got := e.results(core.ResultFilter{}); len(got) != 0 {
			t.Errorf("Results = %+v, want none", got)
		}
		got, err := e.s.Promotions(e.ctx)
		if err != nil || len(got) != 0 {
			t.Errorf("Promotions = %+v, %v; want none and no error", got, err)
		}
	})

	t.Run("lists results oldest saved first, filtered by commit and key", func(t *testing.T) {
		e := newEnv(t, newStore)
		slow := e.submit(e.measure(1, "key-a", 3)).Job
		fast := e.submit(e.measure(2, "key-b", 3)).Job
		other := e.submit(e.check(2, "key-c")).Job
		for _, j := range []core.Job{fast, other, slow} {
			res := e.resultFor(j, 1, 2, 3)
			e.finish(j.ID, &res)
		}
		var order []core.JobID
		for _, r := range e.results(core.ResultFilter{}) {
			order = append(order, r.Job)
		}
		if want := []core.JobID{fast.ID, other.ID, slow.ID}; !reflect.DeepEqual(order, want) {
			t.Errorf("order = %v, want the order they were saved, %v", order, want)
		}
		if got := e.results(core.ResultFilter{Commit: commit(2)}); len(got) != 2 || got[0].Job != fast.ID || got[1].Job != other.ID {
			t.Errorf("results of commit 2 = %+v, want %s then %s", got, fast.ID, other.ID)
		}
		if got := e.results(core.ResultFilter{Key: "key-a"}); len(got) != 1 || got[0].Job != slow.ID {
			t.Errorf("results under key-a = %+v, want %s", got, slow.ID)
		}
		if got := e.results(core.ResultFilter{Commit: commit(1), Key: "key-b"}); len(got) != 0 {
			t.Errorf("results of commit 1 under key-b = %+v, want none", got)
		}
	})

	t.Run("records promotions in order and never changes one", func(t *testing.T) {
		e := newEnv(t, newStore)
		var want []core.Promotion
		for i := range 4 {
			p := core.Promotion{
				Commit: commit(i + 1), PreviousHead: commit(i), At: e.now(), ResultJob: core.JobID("j1"),
				Verdict:     core.Verdict{Kind: core.Better, CandidateSamples: 5, BaseSamples: 5, CandidateMean: 1.5, BaseMean: 1.25, Ratio: 1.2, Noise: 0.05},
				Environment: core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU},
			}
			if err := e.s.RecordPromotion(e.ctx, p); err != nil {
				t.Fatal(err)
			}
			want = append(want, p)
		}
		got, err := e.s.Promotions(e.ctx)
		if err != nil || !reflect.DeepEqual(got, want) {
			t.Errorf("Promotions = %+v, %v; want %+v", got, err, want)
		}
		got[0].Commit = "changed by the caller"
		again, _ := e.s.Promotions(e.ctx)
		if !reflect.DeepEqual(again, want) {
			t.Errorf("a caller changed a promotion in the store: %+v", again[0])
		}
		wantKind(t, "a promotion with no commit", e.s.RecordPromotion(e.ctx, core.Promotion{At: e.now()}), core.FailureInvalidInput)
		if again, _ := e.s.Promotions(e.ctx); len(again) != 4 {
			t.Errorf("%d promotions after a refused one, want 4", len(again))
		}
	})

	t.Run("returns copies: changing one never changes the store", func(t *testing.T) {
		e := newEnv(t, newStore)
		sub := e.measure(1, "key-a", 3)
		sub.Note = e.note("keep")
		job := e.submit(sub).Job
		res := e.resultFor(job, 1, 2, 3)
		e.finish(job.ID, &res)
		res.Outcome.(core.MeasureOutcome).Samples[0] = 99
		res.Environment.Facts["go"] = "changed"
		got := e.job(job.ID)
		got.Notes[0].Text = "changed"
		got.Spec.Settings = core.CheckSettings{}
		results := e.results(core.ResultFilter{})
		results[0].Outcome.(core.MeasureOutcome).Samples[1] = 99
		results[0].Environment.Facts["go"] = "changed"
		if again := e.job(job.ID); again.Notes[0].Text != "keep" || again.Spec.Settings.Kind() != core.JobMeasure {
			t.Errorf("a caller changed a job in the store: %+v", again)
		}
		stored := e.results(core.ResultFilter{})[0]
		if s := stored.Outcome.(core.MeasureOutcome).Samples; !slices.Equal(s, []float64{1, 2, 3}) || stored.Environment.Facts["go"] != "1.25" {
			t.Errorf("a caller changed a result in the store: %+v", stored)
		}
	})
}

// at returns the instant n seconds after the epoch, in a zone other than UTC.
func at(n int, zone *time.Location) time.Time {
	return epoch.Add(time.Duration(n) * time.Second).In(zone)
}
