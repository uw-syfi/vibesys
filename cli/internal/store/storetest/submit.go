package storetest

import (
	"reflect"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

func testSubmit(t *testing.T, newStore New) {
	t.Run("creates a queued job with what was asked", func(t *testing.T) {
		e := newEnv(t, newStore)
		sub := e.measure(1, "key-a", 5)
		sub.Spec.Environment = core.EnvironmentDocker
		sub.Note = e.note("first idea")
		out := e.submit(sub)
		job := out.Job
		if out.Disposition != core.SubmitCreated {
			t.Errorf("disposition = %s, want created", out.Disposition)
		}
		if job.ID == "" || job.State != core.JobQueued || job.Key != "key-a" || !job.SubmittedAt.Equal(sub.At) {
			t.Errorf("job = %+v, want a queued job with key-a submitted at %v", job, sub.At)
		}
		if !reflect.DeepEqual(job.Spec, sub.Spec) {
			t.Errorf("spec = %+v, want %+v", job.Spec, sub.Spec)
		}
		if len(job.Notes) != 1 || job.Notes[0] != *sub.Note {
			t.Errorf("notes = %+v, want the submission's note", job.Notes)
		}
		if job.Environment != nil || job.StartedAt != nil || job.FinishedAt != nil || job.ResultJob != "" {
			t.Errorf("a queued job has no environment, times or result: %+v", job)
		}
		if got := e.job(job.ID); !reflect.DeepEqual(got, job) {
			t.Errorf("Job(%s) = %+v, want what Submit returned, %+v", job.ID, got, job)
		}
	})

	t.Run("gives each job its own id and lists them oldest first", func(t *testing.T) {
		e := newEnv(t, newStore)
		var ids []core.JobID
		for i := range 6 {
			ids = append(ids, e.submit(e.check(i+1, "key-"+string(rune('a'+i)))).Job.ID)
		}
		seen := map[core.JobID]bool{}
		for _, id := range ids {
			if seen[id] {
				t.Fatalf("id %s was given twice: %v", id, ids)
			}
			seen[id] = true
		}
		var listed []core.JobID
		for _, job := range e.jobs(core.JobFilter{}) {
			listed = append(listed, job.ID)
		}
		if !reflect.DeepEqual(listed, ids) {
			t.Errorf("Jobs = %v, want submission order %v", listed, ids)
		}
	})

	t.Run("joins a queued job with the same key", func(t *testing.T) {
		e := newEnv(t, newStore)
		first := e.submit(e.check(1, "key-a"))
		again := e.submit(e.check(2, "key-a"))
		if again.Disposition != core.SubmitJoined || again.Job.ID != first.Job.ID {
			t.Errorf("second submission = %s on %s, want joined on %s", again.Disposition, again.Job.ID, first.Job.ID)
		}
		if n := len(e.jobs(core.JobFilter{})); n != 1 {
			t.Errorf("%d jobs, want 1", n)
		}
	})

	t.Run("joins a running job with the same key", func(t *testing.T) {
		e := newEnv(t, newStore)
		first := e.submit(e.measure(1, "key-a", 3))
		e.move(first.Job.ID, core.JobQueued, core.JobRunning)
		again := e.submit(e.measure(1, "key-a", 3))
		if again.Disposition != core.SubmitJoined || again.Job.ID != first.Job.ID || again.Job.State != core.JobRunning {
			t.Errorf("second submission = %s on %s in %s, want joined on the running %s", again.Disposition, again.Job.ID, again.Job.State, first.Job.ID)
		}
	})

	t.Run("joins by key and by nothing else", func(t *testing.T) {
		e := newEnv(t, newStore)
		a := e.submit(e.check(1, "key-a"))
		b := e.submit(e.check(1, "key-b"))
		c := e.submit(e.check(2, "key-a"))
		if b.Disposition != core.SubmitCreated || b.Job.ID == a.Job.ID {
			t.Errorf("a different key on the same commit = %s on %s, want a new job", b.Disposition, b.Job.ID)
		}
		if c.Disposition != core.SubmitJoined || c.Job.ID != a.Job.ID {
			t.Errorf("the same key on another commit = %s on %s, want joined on %s: the key already says what the job is", c.Disposition, c.Job.ID, a.Job.ID)
		}
	})

	t.Run("a note and a plan link land on the job a submission joins", func(t *testing.T) {
		e := newEnv(t, newStore)
		first := e.submit(e.check(1, "key-a"))
		p1 := e.createPlan("one")
		p2 := e.createPlan("two")
		sub := e.check(1, "key-a")
		sub.Note, sub.Plan = e.note("from the second agent"), p1.Number
		joined := e.submit(sub)
		sub2 := e.check(1, "key-a")
		sub2.Note, sub2.Plan = e.note("from the third agent"), p2.Number
		joined2 := e.submit(sub2)
		sub3 := e.check(1, "key-a")
		sub3.Plan = p1.Number
		e.submit(sub3)
		job := e.job(first.Job.ID)
		if !reflect.DeepEqual(job.Plans, []core.PlanNumber{p1.Number, p2.Number}) {
			t.Errorf("plans = %v, want [%d %d] in link order, each once", job.Plans, p1.Number, p2.Number)
		}
		if len(job.Notes) != 2 || job.Notes[0] != *sub.Note || job.Notes[1] != *sub2.Note {
			t.Errorf("notes = %+v, want both notes in order", job.Notes)
		}
		if len(joined.Job.Notes) != 1 || len(joined2.Job.Notes) != 2 {
			t.Errorf("the outcome shows %d then %d notes, want 1 then 2: it is the job after the note landed", len(joined.Job.Notes), len(joined2.Job.Notes))
		}
		if !reflect.DeepEqual(joined2.Job, job) && len(e.jobs(core.JobFilter{})) != 1 {
			t.Errorf("joining made another job")
		}
	})

	t.Run("a created job carries the note and the plan link", func(t *testing.T) {
		e := newEnv(t, newStore)
		plan := e.createPlan("idea")
		sub := e.check(1, "key-a")
		sub.Note, sub.Plan = e.note("why"), plan.Number
		job := e.submit(sub).Job
		if len(job.Notes) != 1 || !reflect.DeepEqual(job.Plans, []core.PlanNumber{plan.Number}) {
			t.Errorf("job = %+v, want the note and plan %d", job, plan.Number)
		}
	})

	t.Run("does not join a job that has finished", func(t *testing.T) {
		// A check or measure job that succeeded has a result, so it is reused and not joined.
		for _, final := range []core.JobState{core.JobFailed, core.JobCancelled, core.JobLost} {
			e := newEnv(t, newStore)
			first := e.submit(e.check(1, "key-a")).Job
			e.move(first.ID, core.JobQueued, core.JobRunning)
			e.move(first.ID, core.JobRunning, final)
			again := e.submit(e.check(1, "key-a"))
			if again.Disposition != core.SubmitCreated || again.Job.ID == first.ID {
				t.Errorf("after the first job %s, a submission = %s on %s, want a new job: only a saved result is reused", final, again.Disposition, again.Job.ID)
			}
		}
	})

	t.Run("joins the oldest of several jobs in flight", func(t *testing.T) {
		e := newEnv(t, newStore)
		first := e.submit(e.check(1, "key-a"))
		sub := e.check(1, "key-a")
		sub.NoCache = true
		second := e.submit(sub)
		third := e.submit(e.check(1, "key-a"))
		if second.Disposition != core.SubmitCreated || third.Disposition != core.SubmitJoined || third.Job.ID != first.Job.ID {
			t.Errorf("NoCache = %s, then a plain submission = %s on %s; want created, then joined on %s", second.Disposition, third.Disposition, third.Job.ID, first.Job.ID)
		}
	})

	t.Run("does not join a job in flight that takes fewer samples than asked for", func(t *testing.T) {
		e := newEnv(t, newStore)
		small := e.submit(e.measure(1, "key-a", 3))
		big := e.submit(e.measure(1, "key-a", 9))
		if big.Disposition != core.SubmitCreated || big.Job.ID == small.Job.ID {
			t.Fatalf("a submission for 9 samples = %s on %s, want a new job: the job in flight takes 3", big.Disposition, big.Job.ID)
		}
		e.move(small.Job.ID, core.JobQueued, core.JobRunning)
		if out := e.submit(e.measure(1, "key-a", 3)); out.Disposition != core.SubmitJoined || out.Job.ID != small.Job.ID {
			t.Errorf("a submission for 3 samples = %s on %s, want joined on the oldest job that takes enough, %s", out.Disposition, out.Job.ID, small.Job.ID)
		}
		if out := e.submit(e.measure(1, "key-a", 5)); out.Disposition != core.SubmitJoined || out.Job.ID != big.Job.ID {
			t.Errorf("a submission for 5 samples = %s on %s, want joined on %s, which takes 9", out.Disposition, out.Job.ID, big.Job.ID)
		}
		recheck := e.measure(1, "key-a", 3)
		recheck.Spec.Settings = core.MeasureSettings{Samples: 3, Recheck: true}
		if out := e.submit(recheck); out.Disposition != core.SubmitCreated {
			t.Errorf("a submission that rechecks = %s on %s, want a new job: no job in flight carries a fresh check", out.Disposition, out.Job.ID)
		}
	})

	t.Run("a submission that leaves the samples to the task joins by the task's default", func(t *testing.T) {
		e := newEnv(t, newStore, store.Rules{DefaultSamples: 5})
		first := e.submit(e.measure(1, "key-a", 0))
		if out := e.submit(e.measure(1, "key-a", 4)); out.Disposition != core.SubmitJoined || out.Job.ID != first.Job.ID {
			t.Errorf("a submission for 4 samples = %s on %s, want joined on %s, which takes the default 5", out.Disposition, out.Job.ID, first.Job.ID)
		}
		if out := e.submit(e.measure(1, "key-a", 0)); out.Disposition != core.SubmitJoined || out.Job.ID != first.Job.ID {
			t.Errorf("a submission for the default = %s on %s, want joined on %s", out.Disposition, out.Job.ID, first.Job.ID)
		}
		if out := e.submit(e.measure(1, "key-a", 6)); out.Disposition != core.SubmitCreated {
			t.Errorf("a submission for 6 samples = %s on %s, want a new job: more than the default 5", out.Disposition, out.Job.ID)
		}
		none := newEnv(t, newStore)
		a, b := none.submit(none.measure(1, "key-a", 0)), none.submit(none.measure(1, "key-a", 0))
		if a.Disposition != core.SubmitCreated || b.Disposition != core.SubmitCreated || a.Job.ID == b.Job.ID {
			t.Errorf("with no default, two submissions with no sample count = %s on %s and %s on %s, want two new jobs", a.Disposition, a.Job.ID, b.Disposition, b.Job.ID)
		}
	})

	t.Run("joins exactly as core.CanJoin says, and the oldest job it accepts", func(t *testing.T) {
		joined, created := 0, 0
		for seed := range uint64(20) {
			g := newGen(seed)
			defaultSamples := uint32(g.pick(5))
			e := newEnv(t, newStore, store.Rules{DefaultSamples: defaultSamples})
			for range 40 {
				s := e.measure(1, []string{"key-a", "key-a", "key-b"}[g.pick(3)], 0)
				switch g.pick(5) {
				case 0:
					s.Spec.Settings = core.CheckSettings{}
				case 1:
					s.Spec.Settings = core.CommandSettings{Argv: []string{"ls"}}
					s.Key = ""
				default:
					s.Spec.Settings = core.MeasureSettings{Samples: uint32(g.pick(6)), Recheck: g.pick(2) == 0}
				}
				s.NoCache = g.pick(6) == 0
				var want core.JobID
				for _, j := range e.jobs(core.JobFilter{}) {
					if core.CanJoin(j, s, defaultSamples) {
						want = j.ID
						break
					}
				}
				out := e.submit(s)
				switch {
				case want != "" && (out.Disposition != core.SubmitJoined || out.Job.ID != want):
					t.Fatalf("seed %d: %+v = %s on %s; core.CanJoin accepts %s", seed, s.Spec.Settings, out.Disposition, out.Job.ID, want)
				case want == "" && out.Disposition != core.SubmitCreated:
					t.Fatalf("seed %d: %+v = %s on %s; core.CanJoin accepts no job", seed, s.Spec.Settings, out.Disposition, out.Job.ID)
				case want != "":
					joined++
				default:
					created++
				}
				if g.pick(4) == 0 { // some jobs run, and some finish, so not every job in flight is queued
					jobs := e.jobs(core.JobFilter{})
					switch j := jobs[g.pick(len(jobs))]; j.State {
					case core.JobQueued:
						e.move(j.ID, core.JobQueued, core.JobRunning)
					case core.JobRunning:
						e.move(j.ID, core.JobRunning, core.JobFailed)
					}
				}
			}
		}
		if joined < 50 || created < 50 {
			t.Errorf("the submissions joined %d times and created %d jobs; the test would not tell the rules apart", joined, created)
		}
	})

	t.Run("NoCache neither joins nor reuses", func(t *testing.T) {
		e := newEnv(t, newStore)
		saved := e.savedJob(e.check(1, "key-a"))
		inFlight := e.submit(e.check(2, "key-b"))
		for _, key := range []string{"key-a", "key-b"} {
			sub := e.check(1, key)
			sub.NoCache = true
			out := e.submit(sub)
			if out.Disposition != core.SubmitCreated || out.Job.ID == saved.ID || out.Job.ID == inFlight.Job.ID {
				t.Errorf("NoCache on %s = %s on %s, want a new job", key, out.Disposition, out.Job.ID)
			}
		}
	})

	t.Run("reuses a finished job whose saved result has the key", func(t *testing.T) {
		e := newEnv(t, newStore)
		saved := e.savedJob(e.measure(1, "key-a", 3), 1, 2, 3)
		sub := e.measure(1, "key-a", 3)
		sub.Note = e.note("asked again")
		out := e.submit(sub)
		if out.Disposition != core.SubmitReused || out.Job.ID != saved.ID || out.Job.State != core.JobSucceeded {
			t.Fatalf("submission = %s on %s in %s, want reused on the finished %s", out.Disposition, out.Job.ID, out.Job.State, saved.ID)
		}
		if len(out.Job.Notes) != 1 || out.Job.Notes[0] != *sub.Note {
			t.Errorf("notes = %+v, want the note appended to the reused job", out.Job.Notes)
		}
		if got := e.job(saved.ID); !reflect.DeepEqual(got, out.Job) {
			t.Errorf("Job = %+v, want %+v", got, out.Job)
		}
		if n := len(e.jobs(core.JobFilter{})); n != 1 {
			t.Errorf("%d jobs, want 1: reuse makes no job", n)
		}
		if n := len(e.results(core.ResultFilter{})); n != 1 {
			t.Errorf("%d results, want 1", n)
		}
	})

	t.Run("reuses the result saved last", func(t *testing.T) {
		e := newEnv(t, newStore)
		e.savedJob(e.check(1, "key-a"))
		again := e.check(1, "key-a")
		again.NoCache = true
		second := e.savedJob(again)
		if out := e.submit(e.check(1, "key-a")); out.Job.ID != second.ID || out.Disposition != core.SubmitReused {
			t.Errorf("submission = %s on %s, want reused on the latest, %s", out.Disposition, out.Job.ID, second.ID)
		}
	})

	t.Run("does not reuse a finished job without a result under the key", func(t *testing.T) {
		e := newEnv(t, newStore)
		ok := e.submit(e.check(1, "key-a")).Job
		e.move(ok.ID, core.JobQueued, core.JobRunning)
		e.move(ok.ID, core.JobRunning, core.JobFailed)
		other := e.savedJob(e.check(1, "key-b"))
		out := e.submit(e.check(1, "key-a"))
		if out.Disposition != core.SubmitCreated || out.Job.ID == ok.ID || out.Job.ID == other.ID {
			t.Errorf("submission = %s on %s, want a new job: the finished job saved no result and the result saved is under another key", out.Disposition, out.Job.ID)
		}
	})

	t.Run("a command job is never joined or reused", func(t *testing.T) {
		e := newEnv(t, newStore)
		first := e.submit(e.command(1, "make", "test"))
		second := e.submit(e.command(1, "make", "test"))
		if first.Disposition != core.SubmitCreated || second.Disposition != core.SubmitCreated || first.Job.ID == second.Job.ID {
			t.Errorf("two identical command submissions = %s on %s and %s on %s, want two created jobs", first.Disposition, first.Job.ID, second.Disposition, second.Job.ID)
		}
		e.finish(first.Job.ID, nil)
		third := e.submit(e.command(1, "make", "test"))
		if third.Disposition != core.SubmitCreated {
			t.Errorf("a command after a finished command = %s, want created", third.Disposition)
		}
		if first.Job.Key != "" {
			t.Errorf("a command job has key %q, want none", first.Job.Key)
		}
	})

	t.Run("the reuse rule it is given decides", func(t *testing.T) {
		var asked []core.Result
		var askedFor []core.Submission
		answer := false
		rule := func(saved core.Result, want core.Submission) bool {
			asked, askedFor = append(asked, saved), append(askedFor, want)
			return answer
		}
		e := newEnv(t, newStore, store.Rules{Reuse: rule})
		e.savedJob(e.measure(1, "key-a", 3), 1, 2, 3)
		wantResult := e.results(core.ResultFilter{})[0]
		sub := e.measure(1, "key-a", 9)
		if out := e.submit(sub); out.Disposition != core.SubmitCreated {
			t.Errorf("with the rule refusing, submission = %s, want created", out.Disposition)
		}
		if len(asked) != 1 || !reflect.DeepEqual(asked[0], wantResult) || !reflect.DeepEqual(askedFor[0].Spec, sub.Spec) {
			t.Errorf("the rule was asked %d times, with %+v for %+v; want once, with the saved result and the submission", len(asked), asked, askedFor)
		}
		answer = true
		if out := e.submit(e.measure(1, "key-a", 3)); out.Disposition != core.SubmitJoined {
			t.Errorf("with a job in flight under the key, submission = %s, want joined: a job in flight is joined before a result is considered", out.Disposition)
		}
		e.savedJob(e.measure(1, "key-c", 3), 4)
		if out := e.submit(e.measure(1, "key-c", 3)); out.Disposition != core.SubmitReused {
			t.Errorf("with the rule accepting, submission = %s, want reused", out.Disposition)
		}
	})

	t.Run("a landing that adds nothing leaves the job as it was", func(t *testing.T) {
		e := newEnv(t, newStore)
		first := e.submit(e.check(1, "key-a")).Job
		again := e.submit(e.check(1, "key-a"))
		if !reflect.DeepEqual(again.Job, first) {
			t.Errorf("joined job = %+v, want %+v", again.Job, first)
		}
		before := snapshot(t, e.s)
		e.submit(e.check(1, "key-a"))
		unchanged(t, "a duplicate submission with no note", e.s, before)
	})

	t.Run("refuses what it cannot hold and changes nothing", func(t *testing.T) {
		e := newEnv(t, newStore)
		plan := e.createPlan("idea")
		keep := e.submit(e.check(1, "key-keep")).Job
		before := snapshot(t, e.s)
		long := strings.Repeat("k", 128)
		cases := []struct {
			name string
			sub  core.Submission
			kind core.FailureKind
		}{
			{"no settings", core.Submission{Spec: core.JobSpec{Commit: commit(1)}, Key: "key-a", At: e.now()}, core.FailureInvalidInput},
			{"no commit", core.Submission{Spec: core.JobSpec{Settings: core.CheckSettings{}}, Key: "key-a", At: e.now()}, core.FailureInvalidInput},
			{"a check with no key", e.check(1, ""), core.FailureInvalidInput},
			{"a command with a key", func() core.Submission { s := e.command(1, "ls"); s.Key = "key-a"; return s }(), core.FailureInvalidInput},
			{"a key with a slash", e.check(1, "a/b"), core.FailureInvalidInput},
			{"a key with a dot dot", e.check(1, ".."), core.FailureInvalidInput},
			{"a key starting with a dot", e.check(1, ".hidden"), core.FailureInvalidInput},
			{"a key with a space", e.check(1, "a b"), core.FailureInvalidInput},
			{"a key of 129 characters", e.check(1, long+"k"), core.FailureInvalidInput},
			{"a note that is not UTF-8", func() core.Submission { s := e.check(1, "key-a"); s.Note = e.note("bad\xff"); return s }(), core.FailureInvalidInput},
			{"a command with a bad argument", e.command(1, "ls", "\xc3\x28"), core.FailureInvalidInput},
			{"a pointer where a value is held", func() core.Submission {
				s := e.check(1, "key-a")
				s.Spec.Settings = &core.CheckSettings{}
				return s
			}(), core.FailureInvalidInput},
			{"a time past year 9999", func() core.Submission { s := e.check(1, "key-a"); s.At = s.At.AddDate(9999, 0, 0); return s }(), core.FailureInvalidInput},
			{"a plan that does not exist", func() core.Submission { s := e.check(1, "key-a"); s.Plan = plan.Number + 7; return s }(), core.FailureNotFound},
		}
		for _, c := range cases {
			_, err := e.s.Submit(e.ctx, c.sub)
			wantKind(t, c.name, err, c.kind)
		}
		unchanged(t, "a refused submission", e.s, before)
		ok := e.check(1, long)
		if out := e.submit(ok); out.Job.Key != core.JobKey(long) {
			t.Errorf("a key of 128 characters was refused or changed: %q", out.Job.Key)
		}
		if got := e.job(keep.ID); !reflect.DeepEqual(got, keep) {
			t.Errorf("an earlier job changed: %+v", got)
		}
	})
}

func (e *env) createPlan(title string) core.Plan {
	e.t.Helper()
	plan, err := e.s.CreatePlan(e.ctx, core.NewPlan{Title: title, Body: "body of " + title, At: e.now()})
	if err != nil {
		e.t.Fatalf("CreatePlan(%q): %v", title, err)
	}
	return plan
}
