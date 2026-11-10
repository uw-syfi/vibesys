package alloc_test

import (
	"errors"
	"fmt"
	"math/rand/v2"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// chooser is where a generated history's choices come from: a seeded source, or a fuzzer's bytes.
type chooser interface{ IntN(n int) int }

// bytesChooser reads choices from bytes, and chooses 0 once they run out, so every input is a
// history.
type bytesChooser struct {
	b []byte
	i int
}

func (c *bytesChooser) IntN(n int) int {
	if c.i >= len(c.b) {
		return 0
	}
	v := int(c.b[c.i])
	c.i++
	return v % n
}

func (c *bytesChooser) done() bool { return c.i >= len(c.b) }

// gen makes the events of a history: plausible ones mostly, as a backend and an agent would send
// them, and junk among them (unknown IDs, other runs, observations out of order, durations a
// request may not have), with time passing between them, unreachable windows, early starts and
// cancels outside the CLI.
type gen struct {
	c    chooser
	jobs int
}

func pick[T any](c chooser, xs ...T) T { return xs[c.IntN(len(xs))] }

var runs = []core.RunID{"run-a", "run-b"}

func (g *gen) id(s alloc.State) alloc.ID {
	all := s.Allocations()
	if len(all) == 0 || g.c.IntN(10) == 0 {
		return alloc.ID(fmt.Sprintf("a%d", 1+g.c.IntN(len(all)+3)))
	}
	return all[g.c.IntN(len(all))].ID
}

// owner is the run of the allocation, mostly, and another sometimes.
func (g *gen) owner(s alloc.State, id alloc.ID) core.RunID {
	if a, ok := s.Get(id); ok && g.c.IntN(8) != 0 {
		return a.Run
	}
	return pick(g.c, runs...)
}

func (g *gen) job(s alloc.State) core.JobID {
	if g.jobs == 0 || g.c.IntN(6) == 0 {
		return core.JobID(fmt.Sprintf("j%d", 1+g.c.IntN(g.jobs+2)))
	}
	// A job some allocation holds, mostly.
	var held []core.JobID
	for _, a := range s.Allocations() {
		if l, ok := liveOf(a); ok {
			if l.Current != nil {
				held = append(held, l.Current.Job)
			}
			for _, q := range l.Queue {
				held = append(held, q.Job)
			}
		}
	}
	if len(held) > 0 && g.c.IntN(4) != 0 {
		return held[g.c.IntN(len(held))]
	}
	return core.JobID(fmt.Sprintf("j%d", 1+g.c.IntN(g.jobs)))
}

// unstarted is a job an allocation started that the shell has not reported started yet.
func (g *gen) unstarted(s alloc.State) (core.JobID, bool) {
	var out []core.JobID
	for _, a := range s.Allocations() {
		if l, ok := liveOf(a); ok && l.Current != nil && l.Current.StartedAt.IsZero() {
			out = append(out, l.Current.Job)
		}
	}
	if len(out) == 0 {
		return "", false
	}
	return out[g.c.IntN(len(out))], true
}

func (g *gen) order(now time.Time) alloc.RenewalOrder {
	o := alloc.RenewalOrder{
		Lead: pick(g.c, 30*time.Minute, time.Hour, 3*time.Hour, 0, 6*time.Hour),
		For:  pick(g.c, 2*time.Hour, 6*time.Hour, 12*time.Hour, 0, 48*time.Hour),
	}
	switch g.c.IntN(6) {
	case 0:
		o.Until = alloc.UntilTime{At: now.Add(pick(g.c, -time.Hour, 0, 2*time.Hour, 10*time.Hour))}
	case 1:
		// No until.
	default:
		o.Until = alloc.UntilRunEnd{}
	}
	return o
}

// next is the time to pass and the event of the next step.
func (g *gen) next(s alloc.State, now time.Time) (time.Duration, alloc.Event) {
	pass := pick(g.c, 0, 0, time.Minute, 10*time.Minute, time.Hour, 3*time.Hour)
	now = now.Add(pass)
	switch g.c.IntN(40) {
	case 0, 1, 2:
		return pass, alloc.Ask{Run: pick(g.c, runs...), Placement: pick(g.c, gpu, gpu, queue, local, off),
			For: pick(g.c, 2*time.Hour, 6*time.Hour, 12*time.Hour, 30*time.Minute, 0, 30*time.Hour)}
	case 3, 4:
		id := g.id(s)
		return pass, alloc.EndAsk{Run: g.owner(s, id), ID: id, AfterJobs: g.c.IntN(2) == 0}
	case 5, 6:
		id := g.id(s)
		return pass, alloc.RenewAsk{Run: g.owner(s, id), ID: id, Order: g.order(now)}
	case 7:
		id := g.id(s)
		return pass, alloc.RenewOffAsk{Run: g.owner(s, id), ID: id}
	case 8, 9, 10, 11, 12:
		into := alloc.ID("")
		switch g.c.IntN(4) {
		case 0, 1:
			into = g.id(s)
		case 2:
			// One whose running job has started, where what is left of its bound decides the fit.
			for _, a := range s.Allocations() {
				if l, ok := liveOf(a); ok && l.Current != nil && !l.Current.StartedAt.IsZero() {
					into = a.ID
				}
			}
		}
		eligible := everywhere
		if g.c.IntN(5) == 0 {
			eligible = []string{pick(g.c, gpu, local, queue)}
		}
		g.jobs++
		j := core.JobID(fmt.Sprintf("j%d", g.jobs))
		if g.c.IntN(8) == 0 {
			j = g.job(s)
		}
		run := pick(g.c, runs...)
		if a, ok := s.Get(into); ok && g.c.IntN(6) != 0 {
			run = a.Run
		}
		bound := pick(g.c, 10*time.Minute, time.Hour, 3*time.Hour, 8*time.Hour, 0)
		if b, ok := g.edge(s, into, now); ok && g.c.IntN(2) == 0 {
			bound = b
		}
		return pass, alloc.JobSubmitted{Run: run, Job: j, Bound: bound, Into: into, Eligible: eligible}
	case 13, 14, 33, 34:
		// Mostly the report the shell sends after a StartJob.
		if j, ok := g.unstarted(s); ok && g.c.IntN(4) != 0 {
			return pass, alloc.JobStarted{Job: j}
		}
		return pass, alloc.JobStarted{Job: g.job(s)}
	case 15, 16, 17:
		return pass, alloc.JobFinished{Job: g.job(s)}
	case 18, 19, 20:
		id := g.id(s)
		h := alloc.Handle{Alloc: id, Ref: "ref-" + string(id)}
		if g.c.IntN(10) == 0 {
			h = pick(g.c, alloc.Handle{Alloc: id}, alloc.Handle{Alloc: "a77", Ref: "x"})
		}
		return pass, alloc.Submitted{ID: id, Handle: h}
	case 21:
		return pass, alloc.SubmitRefused{ID: g.id(s)}
	case 22, 23, 24, 25, 26, 27:
		id := g.id(s)
		return pass, alloc.Observed{ID: id, Seen: g.seen(s, id, now)}
	case 28, 29, 30:
		return pass, alloc.Unreachable{ID: g.id(s)}
	case 31:
		return pass, alloc.Released{ID: g.id(s)}
	case 32:
		if g.c.IntN(4) == 0 {
			return pass, alloc.RunEnded{Run: pick(g.c, runs...)}
		}
	}
	return pass, alloc.Tick{}
}

// edge is a bound at the fit's boundary in a running allocation, the one asked for or any: exactly
// what fits now, or a nanosecond or a minute either side of it.
func (g *gen) edge(s alloc.State, into alloc.ID, now time.Time) (time.Duration, bool) {
	var live []alloc.Allocation
	for _, a := range s.Allocations() {
		if l, ok := liveOf(a); ok && !l.EndsAt.IsZero() && (into == "" || a.ID == into) {
			live = append(live, a)
		}
	}
	if len(live) == 0 {
		return 0, false
	}
	a := live[g.c.IntN(len(live))]
	// Mostly one with a started job ahead, where what is left of its bound decides.
	for _, b := range live {
		if l, _ := liveOf(b); l.Current != nil && !l.Current.StartedAt.IsZero() && g.c.IntN(2) == 0 {
			a = b
			break
		}
	}
	l, _ := liveOf(a)
	margin := config.Placements[a.Placement].Margin
	b := l.EndsAt.Sub(now) - margin - aheadOf(l, now) + pick(g.c, 0, -time.Nanosecond, time.Nanosecond, time.Minute, -time.Minute)
	return b, b > 0
}

// aheadOf is the work ahead of a new job, worked out here and not by the core, so a boundary aimed
// at with it tests the core's.
func aheadOf(l alloc.Live, now time.Time) time.Duration {
	var d time.Duration
	if c := l.Current; c != nil {
		d += c.Bound
		if !c.StartedAt.IsZero() {
			d = max(0, c.StartedAt.Add(c.Bound).Sub(now))
		}
	}
	for _, q := range l.Queue {
		d += q.Bound
	}
	return d
}

// seen is what the backend says of an allocation: mostly what a backend would, given what the core
// last knew, and sometimes anything.
func (g *gen) seen(s alloc.State, id alloc.ID, now time.Time) alloc.Observation {
	a, ok := s.Get(id)
	if ok && g.c.IntN(5) != 0 {
		switch p := a.Phase.(type) {
		case alloc.Pending:
			if g.c.IntN(3) == 0 {
				return alloc.SeenPending{Reason: pick(g.c, "Priority", "Resources")}
			}
			// It starts, now or a little earlier than this look, and maybe says when it ends.
			started := now.Add(-pick(g.c, 0, time.Minute, 2*time.Hour))
			r := alloc.SeenRunning{Node: "node-a", StartedAt: started}
			switch g.c.IntN(3) {
			case 0:
				r.EndsAt = started.Add(a.RequestedFor)
			case 1:
				r.EndsAt = started.Add(a.RequestedFor - pick(g.c, time.Minute, time.Hour, 50*time.Hour))
			}
			return r
		case alloc.Running, alloc.Draining:
			l, _ := liveOf(a)
			if !l.EndsAt.IsZero() && !now.Before(l.EndsAt) {
				return alloc.SeenEnded{Cause: alloc.EndedByTimeLimit, At: l.EndsAt}
			}
			if g.c.IntN(8) == 0 {
				return alloc.SeenEnded{Cause: alloc.EndedOtherwise, At: now}
			}
			ends := l.EndsAt
			if !ends.IsZero() && g.c.IntN(6) == 0 {
				// The backend moves the end earlier, as a site's maintenance would.
				ends = ends.Add(-pick(g.c, 10*time.Minute, time.Hour, 3*time.Hour))
			}
			return alloc.SeenRunning{Node: "node-a", StartedAt: l.StartedAt, EndsAt: ends}
		case alloc.Ended:
			return alloc.SeenEnded{Cause: pick(g.c, alloc.EndedOtherwise, alloc.EndedByTimeLimit), At: p.At}
		}
	}
	switch g.c.IntN(4) {
	case 0:
		return alloc.SeenPending{}
	case 1:
		return alloc.SeenRunning{StartedAt: now.Add(pick(g.c, 0, -time.Hour, time.Hour)), EndsAt: now.Add(pick(g.c, 0, time.Hour, -time.Hour))}
	case 2:
		return alloc.SeenEnded{Cause: alloc.EndedByTimeLimit, At: now.Add(pick(g.c, 0, -time.Hour, time.Hour))}
	}
	return alloc.SeenEnded{Cause: alloc.EndedOtherwise}
}

// history runs a generated history of up to steps events, and after every step checks every
// invariant (checkStep) and the agreement with the model; at the end, it folds the log twice and
// checks the fold is the steps, and the same each time.
func history(c chooser, steps int, until func() bool) error {
	g := &gen{c: c}
	md := newModel(config)
	now := epoch
	var s alloc.State
	var log, durable []alloc.Input
	var all []alloc.Action
	for i := 0; i < steps && (until == nil || !until()); i++ {
		pass, e := g.next(s, now)
		now = now.Add(pass)
		after, actions := alloc.Step(s, config, now, e)
		if err := checkStep(config, now, s, e, after, actions); err != nil {
			return fmt.Errorf("step %d, %T%+v at +%s: %w", i, e, e, now.Sub(epoch), err)
		}
		want := md.step(now, e)
		got := reflect.Type(nil)
		if r, ok := refusal(actions); ok {
			got = reflect.TypeOf(r)
		}
		if got != want {
			return fmt.Errorf("step %d, %T%+v at +%s: the core refused with %v, the model with %v", i, e, e, now.Sub(epoch), got, want)
		}
		if err := md.agree(after); err != nil {
			return fmt.Errorf("step %d, %T%+v at +%s: %w", i, e, e, now.Sub(epoch), err)
		}
		if alloc.Durable(s, after) {
			durable = append(durable, alloc.Input{At: now, Event: e})
		}
		s = after
		log = append(log, alloc.Input{At: now, Event: e})
		all = append(all, actions...)
	}
	// The log the shell keeps has the durable steps alone: folding it gives the state but for the
	// display words of pending allocations.
	if sd, _ := alloc.Fold(config, durable); alloc.Durable(sd, s) {
		return errors.New("folding the durable steps alone gave another state than the steps did")
	}
	s1, a1 := alloc.Fold(config, log)
	s2, a2 := alloc.Fold(config, log)
	if !reflect.DeepEqual(s1, s) || !reflect.DeepEqual(a1, all) {
		return errors.New("folding the log gave another state or other actions than the steps did")
	}
	if !reflect.DeepEqual(s1, s2) || !reflect.DeepEqual(a1, a2) {
		return errors.New("folding the same log twice gave two answers")
	}
	return nil
}

// histories checks seeded histories, and fails naming the seed that broke one.
func histories(t *testing.T, seeds, steps int) {
	t.Helper()
	reached := map[alloc.Stage]int{}
	for seed := range seeds {
		r := rand.New(rand.NewPCG(uint64(seed), 7))
		if err := history(r, steps, nil); err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		// How far the histories reach, so a generator that stopped reaching a stage is noticed.
		s, _ := alloc.Fold(config, replay(rand.New(rand.NewPCG(uint64(seed), 7)), steps))
		for _, a := range s.Allocations() {
			reached[a.Stage()]++
		}
	}
	for _, st := range []alloc.Stage{alloc.StagePending, alloc.StageRunning, alloc.StageEnded} {
		if reached[st] == 0 {
			t.Errorf("no history left an allocation %s: the generator no longer reaches it (%v)", st, reached)
		}
	}
}

// replay is the log of the history a chooser makes, without the checks.
func replay(c chooser, steps int) []alloc.Input {
	g := &gen{c: c}
	now := epoch
	var s alloc.State
	var log []alloc.Input
	for range steps {
		pass, e := g.next(s, now)
		now = now.Add(pass)
		s, _ = alloc.Step(s, config, now, e)
		log = append(log, alloc.Input{At: now, Event: e})
	}
	return log
}

// Random histories of requests, jobs, observations, unreachable windows and time keep every
// invariant after every step, agree with the model, and fold the same every time. The sweep runs
// many more of them without the race detector.
func TestRandomHistoriesKeepEveryInvariant(t *testing.T) {
	histories(t, 30, 300)
}

// FuzzHistory is the same check over histories a fuzzer chooses.
func FuzzHistory(f *testing.F) {
	f.Add([]byte{})
	f.Add([]byte(strings.Repeat("\x00\x08\x16\x12\x0c\x1a", 40)))
	r := rand.New(rand.NewPCG(3, 4))
	for range 8 {
		b := make([]byte, 600)
		for i := range b {
			b[i] = byte(r.IntN(256))
		}
		f.Add(b)
	}
	f.Fuzz(func(t *testing.T, b []byte) {
		c := &bytesChooser{b: b}
		if err := history(c, 400, c.done); err != nil {
			t.Fatal(err)
		}
	})
}

// The generator reaches what the invariants are about: draining, renewals, skipped renewals, jobs
// lost and dropped. A generator that stopped reaching one would leave its checks with nothing to
// check.
func TestTheGeneratorReachesEveryKindOfStep(t *testing.T) {
	seen := map[string]bool{}
	for seed := range 100 {
		log := replay(rand.New(rand.NewPCG(uint64(seed), 7)), 300)
		var s alloc.State
		for _, in := range log {
			var actions []alloc.Action
			s, actions = alloc.Step(s, config, in.At, in.Event)
			for _, a := range actions {
				seen[fmt.Sprintf("%T", a)] = true
			}
			for _, a := range s.Allocations() {
				seen[string(a.Stage())] = true
				if a.RenewedBy != "" {
					seen["renewed"] = true
				}
				if a.RenewalSkipped != "" {
					seen["skipped: "+string(a.RenewalSkipped)] = true
				}
			}
		}
	}
	for _, want := range []string{
		"alloc.Submit", "alloc.Release", "alloc.StartJob", "alloc.LoseJob", "alloc.DropJob", "alloc.NotPlaced",
		"alloc.RecordWait", "alloc.Refuse", "draining", "renewed", "skipped: max_held reached",
	} {
		if !seen[want] {
			t.Errorf("no history reached %s", want)
		}
	}
}
