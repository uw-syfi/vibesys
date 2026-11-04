package storetest

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// hangGuard only turns a call that never returns into a failure. A passing run finishes in a
// tiny fraction of it, and no test waits for it.
const hangGuard = 60 * time.Second

func cancelledContext() (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	return ctx, cancel
}

func isCancelled(err error) bool { return errors.Is(err, context.Canceled) }

// parallel runs fn on n goroutines that start together, once all of them are ready, and returns
// when every one has finished. A call that never returns fails the test.
func parallel(t *testing.T, n int, fn func(i int)) {
	t.Helper()
	var ready, finished sync.WaitGroup
	start := make(chan struct{})
	ready.Add(n)
	finished.Add(n)
	for i := range n {
		go func() {
			defer finished.Done()
			ready.Done()
			<-start
			fn(i)
		}()
	}
	ready.Wait()
	close(start)
	waitWithin(t, &finished)
}

// Parallel runs fn on n goroutines that start together and returns when every one has finished.
// A call that never returns fails the test after a bound that only guards against hangs.
func Parallel(t *testing.T, n int, fn func(i int)) {
	t.Helper()
	parallel(t, n, fn)
}

func waitWithin(t *testing.T, wg *sync.WaitGroup) {
	t.Helper()
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(hangGuard):
		t.Fatalf("a store call did not return within %v", hangGuard)
	}
}

func testConcurrency(t *testing.T, newStore New) {
	const n = 16

	t.Run("parallel duplicate submissions make one job", func(t *testing.T) {
		e := newEnv(t, newStore)
		plans := []core.PlanNumber{e.createPlan("a").Number, e.createPlan("b").Number}
		subs := make([]core.Submission, n)
		for i := range subs {
			subs[i] = e.measure(1, "key-a", 3)
			subs[i].Note = e.note(fmt.Sprintf("note %d", i))
			subs[i].Plan = plans[i%2]
		}
		outs := make([]core.SubmitOutcome, n)
		errs := make([]error, n)
		parallel(t, n, func(i int) { outs[i], errs[i] = e.s.Submit(e.ctx, subs[i]) })
		created := 0
		for i, out := range outs {
			if errs[i] != nil {
				t.Fatalf("Submit %d: %v", i, errs[i])
			}
			if out.Disposition == core.SubmitCreated {
				created++
			}
			if out.Job.ID != outs[0].Job.ID {
				t.Errorf("submission %d landed on %s, submission 0 on %s", i, out.Job.ID, outs[0].Job.ID)
			}
		}
		if created != 1 {
			t.Errorf("%d submissions created a job, want exactly 1", created)
		}
		jobs := e.jobs(core.JobFilter{})
		if len(jobs) != 1 {
			t.Fatalf("%d jobs, want 1", len(jobs))
		}
		var texts []string
		for _, note := range jobs[0].Notes {
			texts = append(texts, note.Text)
		}
		slices.Sort(texts)
		var want []string
		for i := range n {
			want = append(want, fmt.Sprintf("note %d", i))
		}
		slices.Sort(want)
		if !slices.Equal(texts, want) {
			t.Errorf("notes = %v, want every submission's note once", texts)
		}
		if got := slices.Clone(jobs[0].Plans); len(got) != 2 {
			t.Errorf("plans = %v, want both plans once", got)
		}
	})

	t.Run("parallel submissions of different keys make a job each, with distinct ids", func(t *testing.T) {
		e := newEnv(t, newStore)
		subs := make([]core.Submission, n)
		for i := range subs {
			subs[i] = e.check(1, fmt.Sprintf("key-%d", i))
		}
		ids := make([]core.JobID, n)
		parallel(t, n, func(i int) {
			out, err := e.s.Submit(e.ctx, subs[i])
			if err != nil {
				t.Errorf("Submit %d: %v", i, err)
				return
			}
			ids[i] = out.Job.ID
		})
		slices.Sort(ids)
		if len(slices.Compact(slices.Clone(ids))) != n {
			t.Errorf("ids = %v, want %d distinct", ids, n)
		}
		if got := len(e.jobs(core.JobFilter{})); got != n {
			t.Errorf("%d jobs, want %d", got, n)
		}
	})

	t.Run("of parallel identical transitions exactly one applies", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.check(1, "key-a")).Job
		fp := pinned()
		tr := core.Transition{Job: job.ID, From: core.JobQueued, To: core.JobRunning, At: e.now(), Environment: &fp}
		errs := make([]error, n)
		parallel(t, n, func(i int) { _, errs[i] = e.s.Transition(e.ctx, tr) })
		applied := 0
		for _, err := range errs {
			if err == nil {
				applied++
				continue
			}
			wantKind(t, "a losing transition", err, core.FailureConflict)
		}
		if applied != 1 {
			t.Errorf("%d transitions applied, want exactly 1", applied)
		}
	})

	t.Run("parallel plans and events are numbered without gaps or repeats", func(t *testing.T) {
		e := newEnv(t, newStore)
		g := newGen(2)
		drafts := make([]core.EventDraft, n)
		for i := range drafts {
			drafts[i] = g.draft(core.RunID([]string{"run-a", "run-b"}[i%2]), nil)
		}
		numbers, positions := make([]core.PlanNumber, n), make([]uint64, n)
		parallel(t, n, func(i int) {
			plan, err := e.s.CreatePlan(e.ctx, core.NewPlan{Title: fmt.Sprint(i), At: epoch})
			if err != nil {
				t.Errorf("CreatePlan: %v", err)
			}
			numbers[i] = plan.Number
			ev, err := e.s.AppendEvent(e.ctx, drafts[i])
			if err != nil {
				t.Errorf("AppendEvent: %v", err)
			}
			positions[i] = ev.Sequence
		})
		slices.Sort(numbers)
		for i, number := range numbers {
			if number != core.PlanNumber(i+1) {
				t.Fatalf("plan numbers = %v, want 1 to %d", numbers, n)
			}
		}
		for _, run := range []core.RunID{"run-a", "run-b"} {
			events, err := e.s.Events(e.ctx, run, 0, 0)
			if err != nil || len(events) != n/2 {
				t.Fatalf("Events(%s) = %d, %v; want %d", run, len(events), err, n/2)
			}
			for i, ev := range events {
				if ev.Sequence != uint64(i+1) {
					t.Errorf("%s: event %d has position %d", run, i, ev.Sequence)
				}
			}
		}
	})

	t.Run("parallel log appends each land whole", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.command(1, "ls")).Job
		const size = 24
		parallel(t, n, func(i int) {
			if err := e.s.AppendLog(e.ctx, job.ID, bytes.Repeat([]byte{byte('a' + i)}, size)); err != nil {
				t.Errorf("AppendLog: %v", err)
			}
		})
		chunk, err := e.s.ReadLog(e.ctx, job.ID, 0, 0)
		if err != nil || len(chunk.Data) != n*size {
			t.Fatalf("ReadLog = %d bytes, %v; want %d", len(chunk.Data), err, n*size)
		}
		seen := map[byte]bool{}
		for off := 0; off < len(chunk.Data); off += size {
			block := chunk.Data[off : off+size]
			if !bytes.Equal(block, bytes.Repeat(block[:1], size)) || seen[block[0]] {
				t.Fatalf("the log has a torn or repeated append at %d: %q", off, block)
			}
			seen[block[0]] = true
		}
	})

	t.Run("a reader never sees a result without its state, nor the state without its result", func(t *testing.T) {
		e := newEnv(t, newStore)
		const jobs = 12
		var ids []core.JobID
		for i := range jobs {
			ids = append(ids, e.submit(e.measure(1, fmt.Sprintf("key-%d", i), 3)).Job.ID)
		}
		results := make([]core.Result, jobs)
		for i, id := range ids {
			results[i] = e.resultFor(e.job(id), 1, 2, 3)
		}
		done := make(chan struct{})
		var readers sync.WaitGroup
		readers.Add(2)
		problems := make(chan string, 8)
		report := func(format string, args ...any) {
			select {
			case problems <- fmt.Sprintf(format, args...):
			default:
			}
		}
		// Each reader reads until the writer has finished, so a reader never outlives the test.
		go func() {
			defer readers.Done()
			for open := true; open; {
				select {
				case <-done:
					open = false
				default:
				}
				saved, err := e.s.Results(e.ctx, core.ResultFilter{})
				if err != nil {
					report("Results: %v", err)
					return
				}
				now, err := e.s.Jobs(e.ctx, core.JobFilter{})
				if err != nil {
					report("Jobs: %v", err)
					return
				}
				for _, r := range saved {
					if j := now[slices.IndexFunc(now, func(j core.Job) bool { return j.ID == r.Job })]; j.State != core.JobSucceeded {
						report("the result of %s was visible while the job was %s", r.Job, j.State)
					}
				}
			}
		}()
		go func() {
			defer readers.Done()
			for open := true; open; {
				select {
				case <-done:
					open = false
				default:
				}
				before, err := e.s.Jobs(e.ctx, core.JobFilter{States: []core.JobState{core.JobSucceeded}})
				if err != nil {
					report("Jobs: %v", err)
					return
				}
				saved, err := e.s.Results(e.ctx, core.ResultFilter{})
				if err != nil {
					report("Results: %v", err)
					return
				}
				for _, j := range before {
					if !slices.ContainsFunc(saved, func(r core.Result) bool { return r.Job == j.ID }) {
						report("job %s was visible as succeeded without its result", j.ID)
					}
				}
			}
		}()
		for i, id := range ids {
			e.finish(id, &results[i])
		}
		close(done)
		waitWithin(t, &readers)
		close(problems)
		for p := range problems {
			t.Error(p)
		}
	})

	t.Run("reads during appends see a whole prefix", func(t *testing.T) {
		e := newEnv(t, newStore)
		job := e.submit(e.command(1, "ls")).Job
		g := newGen(4)
		const appends = 20
		done := make(chan struct{})
		var reader sync.WaitGroup
		reader.Add(1)
		problems := make(chan string, 4)
		go func() {
			defer reader.Done()
			for open := true; open; {
				select {
				case <-done:
					open = false
				default:
				}
				events, err := e.s.Events(e.ctx, "run-a", 0, 0)
				if err != nil {
					problems <- err.Error()
					return
				}
				for i, ev := range events {
					if ev.Sequence != uint64(i+1) {
						problems <- fmt.Sprintf("event %d has position %d", i, ev.Sequence)
						return
					}
				}
				chunk, err := e.s.ReadLog(e.ctx, job.ID, 0, 0)
				if err != nil || len(chunk.Data)%4 != 0 {
					problems <- fmt.Sprintf("ReadLog = %d bytes, %v; want whole appends of 4 bytes", len(chunk.Data), err)
					return
				}
			}
		}()
		for range appends {
			if _, err := e.s.AppendEvent(e.ctx, g.draft("run-a", nil)); err != nil {
				t.Fatal(err)
			}
			if err := e.s.AppendLog(e.ctx, job.ID, []byte("abcd")); err != nil {
				t.Fatal(err)
			}
		}
		close(done)
		waitWithin(t, &reader)
		close(problems)
		for p := range problems {
			t.Error(p)
		}
	})
}
