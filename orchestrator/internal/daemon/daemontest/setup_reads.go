package daemontest

import (
	"context"
	"slices"
	"sync"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/setup"
)

// FakeSetupReads answers setup reads from plain facts a test edits, the way the CLI would answer
// from the working tree, git and the job store. A read sees the facts as they were when it started,
// so one that is held and released later is an old read. It runs no goroutine: a read completes
// inside Start, or inside Release for one held by Block.
type FakeSetupReads struct {
	mu      sync.Mutex
	facts   setup.Facts
	detail  string // unreadable when set
	failure error
	blocked bool
	held    []held
	calls   []daemon.SetupRef
}

type held struct {
	read setup.Read
	done func(setup.Read)
}

var _ daemon.SetupReads = (*FakeSetupReads)(nil)

// NewFakeSetupReads starts from facts.
func NewFakeSetupReads(facts setup.Facts) *FakeSetupReads { return &FakeSetupReads{facts: facts} }

// Start takes a snapshot of the facts for ref and completes the read with it, or holds it while
// blocked. A failure set with Fail completes it as a failed read.
func (f *FakeSetupReads) Start(_ context.Context, ref daemon.SetupRef, done func(setup.Read)) {
	f.mu.Lock()
	f.calls = append(f.calls, ref)
	read := setup.Read{Task: ref.Task, Kind: setup.ReadFacts, Facts: clone(f.facts)}
	switch {
	case f.failure != nil:
		read = setup.Read{Task: ref.Task, Kind: setup.ReadFailure, Detail: f.failure.Error()}
	case f.detail != "":
		read = setup.Read{Task: ref.Task, Kind: setup.ReadUnreadable, Detail: f.detail}
	}
	if f.blocked {
		f.held = append(f.held, held{read, done})
		f.mu.Unlock()
		return
	}
	f.mu.Unlock()
	done(read)
}

func clone(f setup.Facts) setup.Facts {
	f.Jobs = slices.Clone(f.Jobs)
	f.Measurements = slices.Clone(f.Measurements)
	f.Decl.Objectives = slices.Clone(f.Decl.Objectives)
	return f
}

// Calls lists the reads started, in order.
func (f *FakeSetupReads) Calls() []daemon.SetupRef {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.calls)
}

// Set replaces the facts.
func (f *FakeSetupReads) Set(facts setup.Facts) { f.edit(func(s *setup.Facts) { *s = facts }) }

// EditTask is a person editing the task in the working tree: its version is now version.
func (f *FakeSetupReads) EditTask(version string) {
	f.edit(func(s *setup.Facts) { s.Version = version })
}

// Commit moves the base to commit, with the task as committed there at version.
func (f *FakeSetupReads) Commit(commit, version string) {
	f.edit(func(s *setup.Facts) { s.Base = setup.Base{Commit: commit, Version: version} })
}

// FinishJob records a job, replacing the one with its id.
func (f *FakeSetupReads) FinishJob(job setup.Job) {
	f.edit(func(s *setup.Facts) {
		i := slices.IndexFunc(s.Jobs, func(j setup.Job) bool { return j.ID == job.ID })
		if i < 0 {
			s.Jobs = append(s.Jobs, job)
			return
		}
		s.Jobs[i] = job
	})
}

// Measure records a base measurement.
func (f *FakeSetupReads) Measure(m setup.Measurement) {
	f.edit(func(s *setup.Facts) { s.Measurements = append(s.Measurements, m) })
}

// Unreadable makes later reads answer that the task cannot be read, for detail; "" undoes it.
func (f *FakeSetupReads) Unreadable(detail string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.detail = detail
}

// Fail makes later reads fail with err; nil undoes it.
func (f *FakeSetupReads) Fail(err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.failure = err
}

// Block holds every read started from now on until Release.
func (f *FakeSetupReads) Block() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.blocked = true
}

// Held is how many reads are waiting.
func (f *FakeSetupReads) Held() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.held)
}

// Release stops blocking and completes every held read, oldest first.
func (f *FakeSetupReads) Release() {
	f.mu.Lock()
	f.blocked = false
	reads := f.held
	f.held = nil
	f.mu.Unlock()
	for _, h := range reads {
		h.done(h.read)
	}
}

// ReleaseAt completes the i-th held read (oldest is 0) and keeps blocking, so a test can finish an
// old read after a newer one.
func (f *FakeSetupReads) ReleaseAt(i int) {
	f.mu.Lock()
	h := f.held[i]
	f.held = slices.Delete(slices.Clone(f.held), i, i+1)
	f.mu.Unlock()
	h.done(h.read)
}

func (f *FakeSetupReads) edit(change func(*setup.Facts)) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.facts = clone(f.facts)
	change(&f.facts)
}
