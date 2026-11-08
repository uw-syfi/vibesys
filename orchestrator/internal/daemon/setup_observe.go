package daemon

import (
	"context"
	"slices"
	"strings"
	"sync"
	"sync/atomic"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/setup"
)

// MaxSetupReads is how many setup reads may be outstanding in the host at once, whatever the number of
// runs in setup. A run holds one at a time besides.
const MaxSetupReads = 2

// This file is the daemon's side of watching a run's setup. A run in setup that has a declared task
// is read now and then: the read asks the job tool's daemon for the facts the six checks are derived
// from, and its answer is put in the setup store, which the run's summary reads from memory.
//
// The loop never waits on a read. observeSetup starts one with SetupReads.Start, which returns at
// once, and the answer arrives through a callback from any goroutine: it goes into the store, which
// has its own mutex, and into an inbox that the next step drains. No read is made, and no store is
// called, with the engine's lock held except to take a view of the runs, so the lock order is the
// engine's and then the store's, and the store never takes the engine's.
//
// When to read is setup.Next's: a trigger (an event of the run, or a change of its job log) makes one
// more read, however many arrive during a read or inside the minimum gap, and a run nothing happens in
// is read less and less often.

// setupWatch is what the daemon holds for one run it reads.
type setupWatch struct {
	// task is the declared name the schedule is for. A new name starts a new schedule.
	task     string
	schedule setup.Schedule
	// held is the events that Next has not been given because the host's cap kept the read from
	// starting, so they apply when it can.
	held setup.Events
	// inFlight is the number of the read in flight, zero when none.
	inFlight uint64
	// touched and searched are what the run's events and job log stood at when last looked at.
	touched, searched uint64
}

// setupFinish is the end of one read, for the schedule.
type setupFinish struct {
	run     core.RunID
	seq     uint64
	outcome setup.Outcome
}

// setupInbox carries the ends of reads from whatever goroutine ran them to the loop.
type setupInbox struct {
	mu       sync.Mutex
	finished []setupFinish
	// outstanding counts the reads started and not ended, including those of a name that was replaced.
	outstanding atomic.Int32
}

func (b *setupInbox) finish(f setupFinish) {
	b.mu.Lock()
	b.finished = append(b.finished, f)
	b.mu.Unlock()
	b.outstanding.Add(-1)
}

func (b *setupInbox) take() []setupFinish {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := b.finished
	b.finished = nil
	return out
}

// setupRun is what observeSetup takes from the engine of a run to read.
type setupRun struct {
	run     core.Run
	touched uint64
}

// liveSetupRuns lists the runs to observe, by id, at one instant of the log: live runs in setup that
// have a declared task.
func (d *Daemon) liveSetupRuns() []setupRun {
	var watch []setupRun
	_ = d.Cut(func(state core.State, _ uint64) error {
		for _, r := range state.Runs() {
			if !r.Phase.Optimizing() && r.Status.Live() && r.SetupTask != "" {
				watch = append(watch, setupRun{run: r, touched: d.touched[r.ID]})
			}
		}
		return nil
	})
	slices.SortFunc(watch, func(a, b setupRun) int { return strings.Compare(string(a.run.ID), string(b.run.ID)) })
	return watch
}

// observeSetup starts the reads that are due. It reports whether it started one; what a read finds is
// shown by publishSetup.
func (d *Daemon) observeSetup(ctx context.Context) (bool, error) {
	if d.cfg.SetupReads == nil || d.Setup == nil {
		return false, nil
	}
	now := d.Clock.Now()
	finished := map[core.RunID][]setupFinish{}
	for _, f := range d.setupInbox.take() {
		finished[f.run] = append(finished[f.run], f)
	}
	watch := d.liveSetupRuns()
	watching := map[core.RunID]bool{}
	for _, w := range watch {
		watching[w.run.ID] = true
	}
	for id := range d.setupWatches {
		// A run that left setup, ended or was stopped is not observed. One that was stopped keeps its
		// last observation for as long as this process lives.
		if !watching[id] {
			delete(d.setupWatches, id)
		}
	}
	started := false
	for _, w := range watch {
		sw := d.setupWatches[w.run.ID]
		if sw == nil || sw.task != w.run.SetupTask {
			// A new run, or a new name: the old read, if one is still out, is another name's.
			sw = &setupWatch{task: w.run.SetupTask}
			d.setupWatches[w.run.ID] = sw
		}
		ev := sw.held
		sw.held = setup.Events{}
		for _, f := range finished[w.run.ID] {
			if f.seq == sw.inFlight {
				ev.Finished, sw.inFlight = f.outcome, 0
			}
		}
		searched := uint64(0)
		if d.Search != nil {
			searched = d.Search.Revision(w.run.ID)
		}
		if w.touched != sw.touched || searched != sw.searched {
			ev.Triggered = true
		}
		sw.touched, sw.searched = w.touched, searched
		next, act := setup.Next(sw.schedule, now, ev)
		if act.Read && d.setupInbox.outstanding.Load() >= MaxSetupReads {
			// The host is full: nothing is lost, the schedule stays as it was and the events wait.
			sw.held = ev
			continue
		}
		sw.schedule = next
		if act.Read {
			sw.inFlight = d.startSetupRead(ctx, w.run)
			started = true
		}
	}
	return started, nil
}

// startSetupRead begins a read of the run's task and returns its number.
func (d *Daemon) startSetupRead(ctx context.Context, run core.Run) uint64 {
	d.setupSeq++
	seq, task, id := d.setupSeq, run.SetupTask, run.ID
	bctx, cancel := d.cfg.Bounds.Bound(ctx)
	d.setupInbox.outstanding.Add(1)
	var once sync.Once
	d.cfg.SetupReads.Start(bctx, SetupRef{JobTool: run.JobTool, Repository: run.Repository, Task: task}, func(r setup.Read) {
		once.Do(func() {
			cancel()
			r.Seq, r.Task = seq, task
			changed := d.Setup.Put(id, r)
			outcome := setup.ReadUnchanged
			switch {
			case r.Kind == setup.ReadFailure:
				outcome = setup.ReadFailed
			case changed:
				outcome = setup.ReadChanged
			}
			d.setupInbox.finish(setupFinish{run: id, seq: seq, outcome: outcome})
		})
	})
	return seq
}

// publishSetup tells the hub about each run whose setup observation moved since the last time, so
// that the streams work its summary out again. It reports whether it told it of any.
func (d *Daemon) publishSetup() bool {
	if d.Setup == nil {
		return false
	}
	moved := false
	for _, r := range d.State().Runs() {
		revision := d.Setup.Revision(r.ID)
		if r.Phase.Optimizing() && revision != 0 {
			// The run left setup, whose summary no longer reads the observation.
			d.Setup.Forget(r.ID)
			revision = 0
		}
		switch {
		case revision == 0:
			delete(d.setupPublished, r.ID)
		case revision > d.setupPublished[r.ID]:
			d.Hub.PublishSetup(r.ID, revision)
			d.setupPublished[r.ID] = revision
			moved = true
		}
	}
	return moved
}
