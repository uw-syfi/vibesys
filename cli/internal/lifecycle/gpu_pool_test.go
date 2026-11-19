package lifecycle_test

import (
	"fmt"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// These tests run the machine over a placement whose GPUs are a pool of numbered devices, and hold
// every grant it gives to the rules a job's GPUs follow: a job gets as many devices as it claims,
// devices of the pool and no one else's, and what a restart finds in the store is what the job
// holds, wherever the daemon died between choosing devices and starting the job.

const (
	poolID    schedule.ResourceID = "gpu:local"
	poolPlace string              = "local"
	// neuronPoolID is a pool of NeuronCores, whose kind gives a job a contiguous range of them.
	neuronPoolID schedule.ResourceID = "neuron:local"
)

func poolScheduling(size int, pool schedule.ResourceID) lifecycle.Scheduling {
	return lifecycle.Scheduling{Resources: []schedule.Resource{
		{ID: schedule.PlacementOf(poolPlace), Capacity: size},
		{ID: pool, Capacity: size, Devices: true},
	}}
}

func gpuCountOf(t *testing.T, n int) core.GPUCount {
	t.Helper()
	c, err := core.ParseGPUCount(uint32(n), "test")
	if err != nil {
		t.Fatal(err)
	}
	return c
}

// poolWork is the work of a job that claims n devices of the pool, or all of them alone for a
// measure (n is then the pool's size), as schedule.PlacementDemand makes it.
func poolWork(pool schedule.ResourceID, n int, whole bool, run core.RunID) lifecycle.Work {
	kind := core.JobCheck
	if whole {
		kind = core.JobMeasure
	}
	claims := []schedule.Claim{
		{Resource: schedule.PlacementOf(poolPlace), Amount: 1, Exclusive: whole},
		{Resource: pool, Amount: n, Exclusive: whole},
	}
	return lifecycle.Work{Kind: kind, Run: run, Options: []schedule.Option{{Placement: poolPlace, Claims: claims}}}
}

type jobStatus int

const (
	statusQueued    jobStatus = iota // in the store as queued, with no supervisor
	statusLaunching                  // launch asked for, not answered: a supervisor is parked, the store has it queued
	statusRunning                    // the store has it running, with the grant recorded
	statusEnded
)

// poolWorld is the model of the shell around a machine with a pool: what the store records of each
// job, and which jobs hold devices.
type poolWorld struct {
	t     *testing.T
	size  int
	pool  schedule.ResourceID
	state lifecycle.State
	now   time.Time
	order []lifecycle.Ref
	work  map[lifecycle.Ref]lifecycle.Work
	// status is what each job's store and supervisor are; recorded is the grant its store has,
	// written when its launch is answered; asked is the grant its launch request carried.
	status   map[lifecycle.Ref]jobStatus
	recorded map[lifecycle.Ref]*core.Grant
	asked    map[lifecycle.Ref]*core.Grant
	running  []lifecycle.Ref
	closing  []lifecycle.Ref
	// holding are the jobs that hold devices: from the launch request until the end is recorded.
	holding map[lifecycle.Ref]*core.Grant
	// log is every grant given, in order, for the test that runs a seed twice.
	log []string
}

func newPoolWorld(t *testing.T, size int) *poolWorld { return newPoolWorldOf(t, size, poolID) }

func newPoolWorldOf(t *testing.T, size int, pool schedule.ResourceID) *poolWorld {
	w := &poolWorld{
		t: t, size: size, pool: pool, now: epoch,
		work: map[lifecycle.Ref]lifecycle.Work{}, status: map[lifecycle.Ref]jobStatus{},
		recorded: map[lifecycle.Ref]*core.Grant{}, asked: map[lifecycle.Ref]*core.Grant{}, holding: map[lifecycle.Ref]*core.Grant{},
	}
	w.state, _ = lifecycle.Step(lifecycle.RecoveringWith(poolScheduling(size, pool)), lifecycle.RecoveryDone{})
	return w
}

func (w *poolWorld) tick() time.Time {
	w.now = w.now.Add(time.Second)
	return w.now
}

// feed gives the machine an event and holds what it asks to the rules of a grant.
func (w *poolWorld) feed(e lifecycle.Event) {
	w.t.Helper()
	var reqs []lifecycle.Request
	w.state, reqs = lifecycle.Step(w.state, e)
	for _, req := range reqs {
		switch r := req.(type) {
		case lifecycle.LaunchRequest:
			w.checkGrant(r)
			w.status[r.Ref] = statusLaunching
			w.asked[r.Ref] = r.GPUs
			w.holding[r.Ref] = r.GPUs
		case lifecycle.Adopt:
			if !slices.Contains(w.running, r.Ref) {
				w.running = append(w.running, r.Ref)
			}
		case lifecycle.Move:
			w.closing = append(w.closing, r.Ref)
		case lifecycle.Discard:
			// The parked supervisor a crash left is released, and the job goes back to the queue.
			defer w.feed(lifecycle.Discarded{Ref: r.Ref})
		}
	}
	w.checkHeld(e)
}

// checkGrant holds a launch to the work of its job: as many devices as it claims, of the pool, the
// whole pool for a claim made alone, and none for a job that claims none of it.
func (w *poolWorld) checkGrant(r lifecycle.LaunchRequest) {
	w.t.Helper()
	claims := w.work[r.Ref].Options[0].Claims
	i := slices.IndexFunc(claims, func(c schedule.Claim) bool { return c.Resource == w.pool })
	switch {
	case i < 0 && r.GPUs != nil:
		w.t.Fatalf("job %v claims no GPUs and was given %+v", r.Ref, r.GPUs)
	case i < 0:
		return
	case r.GPUs == nil:
		w.t.Fatalf("job %v claims %d GPUs and was given none", r.Ref, claims[i].Amount)
	}
	g, claim := r.GPUs, claims[i]
	switch {
	case g.Pool != core.PoolID(w.pool) || g.Size.Int() != w.size:
		w.t.Fatalf("job %v got the grant %+v, want the pool %s of %d", r.Ref, g, w.pool, w.size)
	case g.Pool.Kind().Contiguous() && !g.Devices.IsRange():
		w.t.Fatalf("job %v was given %v of a pool of ranges", r.Ref, g.Devices.Devices())
	case g.Devices.Len() != claim.Amount:
		w.t.Fatalf("job %v claims %d GPUs and was given %v", r.Ref, claim.Amount, g.Devices.Devices())
	case g.Whole != claim.Exclusive:
		w.t.Fatalf("job %v: whole = %v for a claim made alone = %v", r.Ref, g.Whole, claim.Exclusive)
	case g.Source != core.GrantByCLI:
		w.t.Fatalf("job %v: the source of the grant is %q", r.Ref, g.Source)
	}
	for _, d := range g.Devices.Devices() {
		if int(d) >= w.size {
			w.t.Fatalf("job %v was given device %d of a pool of %d", r.Ref, d, w.size)
		}
	}
	w.log = append(w.log, fmt.Sprintf("%v:%v", r.Ref.Job, g.Devices.Devices()))
}

// checkHeld holds every pair of jobs that hold devices to sharing none, and the pool to its size.
func (w *poolWorld) checkHeld(after lifecycle.Event) {
	w.t.Helper()
	var refs []lifecycle.Ref
	for ref, g := range w.holding {
		if g != nil {
			refs = append(refs, ref)
		}
	}
	slices.SortFunc(refs, compareRefs)
	used := 0
	for i, a := range refs {
		used += w.holding[a].Devices.Len()
		for _, b := range refs[i+1:] {
			if w.holding[a].Devices.Overlaps(w.holding[b].Devices) {
				w.t.Fatalf("after %#v: jobs %v and %v hold %v and %v at once", after, a, b, w.holding[a].Devices.Devices(), w.holding[b].Devices.Devices())
			}
		}
	}
	if used > w.size {
		w.t.Fatalf("after %#v: %d devices are held of a pool of %d", after, used, w.size)
	}
}

func compareRefs(a, b lifecycle.Ref) int {
	if a.Store != b.Store {
		return compareStrings(string(a.Store), string(b.Store))
	}
	return compareStrings(string(a.Job), string(b.Job))
}

func compareStrings(a, b string) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	}
	return 0
}

func (w *poolWorld) admit(n int, whole bool, run core.RunID) lifecycle.Ref {
	ref := lifecycle.Ref{Store: lifecycle.StoreIDOf("/repo", "task"), Job: core.JobID(fmt.Sprintf("j%d", len(w.work)+1))}
	if whole {
		n = w.size
	}
	w.work[ref] = poolWork(w.pool, n, whole, run)
	w.status[ref] = statusQueued
	w.order = append(w.order, ref)
	w.feed(lifecycle.Admitted{Ref: ref, SubmittedAt: w.tick(), Work: w.work[ref]})
	return ref
}

// launched answers a launch: the supervisor is up, and the store records the job running with the
// grant it was launched with.
func (w *poolWorld) launched(ref lifecycle.Ref) {
	w.t.Helper()
	w.status[ref], w.recorded[ref] = statusRunning, w.asked[ref]
	w.feed(lifecycle.Launched{Ref: ref})
}

func (w *poolWorld) finish(ref lifecycle.Ref) {
	w.t.Helper()
	w.running = slices.DeleteFunc(w.running, func(x lifecycle.Ref) bool { return x == ref })
	w.feed(lifecycle.Finished{Ref: ref, At: w.tick(), Outcome: lifecycle.Outcome{End: lifecycle.CommandEnded{Attempt: core.Ended{Exec: core.ExecOutcome{Kind: core.ExecExited}}}}})
}

func (w *poolWorld) close(ref lifecycle.Ref) {
	w.t.Helper()
	w.closing = slices.DeleteFunc(w.closing, func(x lifecycle.Ref) bool { return x == ref })
	w.status[ref] = statusEnded
	delete(w.holding, ref)
	w.feed(lifecycle.Closed{Ref: ref})
}

// crash is the daemon dying and starting again: the machine's memory is gone, and what is left is
// the store's records and the supervisors. A job whose launch was asked for and not answered is
// queued in its store, with a parked supervisor; one launched is running in its store, with the
// grant recorded and a supervisor parked or running as parked says; the ones that ended hold nothing.
func (w *poolWorld) crash(parked func(lifecycle.Ref) bool) {
	w.t.Helper()
	w.holding = map[lifecycle.Ref]*core.Grant{}
	w.running, w.closing = nil, nil
	w.state = lifecycle.RecoveringWith(poolScheduling(w.size, w.pool))
	for _, ref := range w.order {
		work := w.work[ref]
		switch w.status[ref] {
		case statusRunning:
			work.Granted = w.recorded[ref]
			w.holding[ref] = w.recorded[ref]
			phase := lifecycle.RunRunning
			if parked(ref) {
				phase = lifecycle.RunParked
			}
			w.feed(lifecycle.Recovered{Ref: ref, Finding: lifecycle.JobIn{State: core.JobRunning, SubmittedAt: epoch, Work: work}, Supervisor: lifecycle.RunAlive{Phase: phase, Limit: time.Hour}, At: w.now})
		case statusLaunching:
			// Nothing ran on the grant the lost launch carried: the store never recorded it.
			w.status[ref] = statusQueued
			w.feed(lifecycle.Recovered{Ref: ref, Finding: lifecycle.JobIn{State: core.JobQueued, SubmittedAt: epoch, Work: work}, Supervisor: lifecycle.RunAlive{Phase: lifecycle.RunParked, Limit: time.Hour}, At: w.now})
		case statusQueued:
			w.feed(lifecycle.Recovered{Ref: ref, Finding: lifecycle.JobIn{State: core.JobQueued, SubmittedAt: epoch, Work: work}, Supervisor: lifecycle.RunNone{}, At: w.now})
		}
	}
	w.feed(lifecycle.RecoveryDone{})
}

// settle answers whatever the machine is waiting on until nothing is, by the generator's choice:
// the launches, the ends and the closes, in any order.
func (w *poolWorld) answerOne(pick func(n int) int) bool {
	type move struct {
		do func()
	}
	var moves []move
	for _, ref := range w.order {
		switch w.status[ref] {
		case statusLaunching:
			moves = append(moves, move{func() { w.launched(ref) }})
		case statusRunning:
			if slices.Contains(w.running, ref) {
				moves = append(moves, move{func() { w.finish(ref) }})
			}
		}
	}
	for _, ref := range w.closing {
		moves = append(moves, move{func() { w.close(ref) }})
	}
	if len(moves) == 0 {
		return false
	}
	moves[pick(len(moves))].do()
	return true
}

// A job is given as many devices as it claims, chosen from those no job holds, in every sequence of
// admissions, launches, ends and daemon deaths, wherever a death falls: between the choice of
// devices and the launch (the job is queued again, and its devices are free), after the store
// records the job running and before its command starts (a parked supervisor, whose devices stay
// its own), or while it runs. No two jobs that hold devices share one, and the grants the machine
// gives from one seed are the same each time.
//
// A pool of NeuronCores keeps the same rules, and every job of it is given a contiguous range.
func TestJobsHoldDisjointDevicesThroughEveryEventSequenceAndCrash(t *testing.T) {
	for seed := uint64(1); seed <= 600; seed++ {
		size := []int{1, 2, 4, 8, 6, 3}[seed%6]
		pool := []schedule.ResourceID{poolID, neuronPoolID}[seed%2]
		var logs [2][]string
		for run := range 2 {
			r := newRand(seed)
			w := newPoolWorldOf(t, size, pool)
			for step := 0; step < 120; step++ {
				switch c := r.IntN(10); {
				case c < 3:
					w.admit(1+r.IntN(size), r.IntN(6) == 0, core.RunID(fmt.Sprintf("run-%d", r.IntN(3))))
				case c == 3 && r.IntN(3) == 0:
					w.crash(func(lifecycle.Ref) bool { return r.IntN(2) == 0 })
				default:
					w.answerOne(r.IntN)
				}
			}
			logs[run] = w.log
		}
		if !slices.Equal(logs[0], logs[1]) {
			t.Fatalf("seed %d: the same events gave the grants %v and then %v", seed, logs[0], logs[1])
		}
	}
}

// A job waiting for devices that are not free is not overtaken by a later job that would take
// the ones that are, so a job of the whole pool is not starved by a stream of small ones; and every
// job gets its devices once the jobs ahead of it end.
func TestAJobOfTheWholePoolIsNotStarvedBySmallOnes(t *testing.T) {
	const size = 8
	w := newPoolWorld(t, size)
	for range size {
		w.admit(1, false, "run-a")
	}
	measure := w.admit(1, true, "run-a")
	var later []lifecycle.Ref
	for range 20 {
		later = append(later, w.admit(1, false, "run-b"))
	}
	started := func(ref lifecycle.Ref) bool { return w.status[ref] != statusQueued }
	for _, ref := range later {
		if started(ref) {
			t.Fatalf("job %v started beside the one-GPU jobs while the measure waits", ref)
		}
	}
	if started(measure) {
		t.Fatal("the measure started while the pool was full")
	}
	for n := 0; n < 500 && !started(measure); n++ {
		if !w.answerOne(func(n int) int { return 0 }) {
			break
		}
		for _, ref := range later {
			if started(ref) && !started(measure) {
				t.Fatalf("job %v of a later arrival started before the measure that waited for the whole pool", ref)
			}
		}
	}
	if !started(measure) {
		t.Fatal("the measure never started")
	}
	g := w.asked[measure]
	if g == nil || !g.Whole || g.Devices.Len() != size {
		t.Fatalf("the measure was given %+v, want all %d devices", g, size)
	}
}

// A job that waits for 4 devices with 2 free says how many it wants, behind which job a later one waits.
func TestAJobThatWaitsForDevicesSaysWhatItWaitsFor(t *testing.T) {
	w := newPoolWorld(t, 8)
	w.admit(6, false, "run-a")
	big := w.admit(4, false, "run-a")
	small := w.admit(1, false, "run-a")
	queue := w.state.Queue()
	if len(queue) != 2 || queue[0].Ref != big || queue[0].Reason.Kind != core.WaitFull || queue[0].Reason.Resource != poolID {
		t.Fatalf("the 4-device job waits as %+v, want a wait on the full pool", queue)
	}
	if queue[1].Ref != small || queue[1].Reason.Kind != core.WaitBehind {
		t.Fatalf("the 1-device job waits as %+v, want behind the 4-device job, though a device is free", queue[1])
	}
}

// A job of a pool of ranges whose free devices are as many as it claims and not in a row waits for
// a range, naming the pool, instead of being given devices with a gap, which the Neuron runtime
// refuses; once a range is free it is given it.
func TestAJobOfAPoolOfRangesWaitsForARange(t *testing.T) {
	w := newPoolWorldOf(t, 4, neuronPoolID)
	var ones []lifecycle.Ref
	for range 3 {
		ones = append(ones, w.admit(1, false, "run-a"))
	}
	for _, ref := range ones {
		w.launched(ref)
	}
	holder := func(d core.DeviceIndex) lifecycle.Ref {
		for ref, g := range w.holding {
			if g != nil && g.Devices.Has(d) {
				return ref
			}
		}
		t.Fatalf("no job holds core %d: %v", d, w.holding)
		return lifecycle.Ref{}
	}
	end := func(ref lifecycle.Ref) {
		w.running = append(w.running, ref)
		w.finish(ref)
		w.close(ref)
	}
	end(holder(1)) // cores 1 and 3 are free, and not in a row
	two := w.admit(2, false, "run-b")
	if g := w.holding[two]; g != nil {
		t.Fatalf("a job of 2 was given %v while cores 1 and 3 were free", g.Devices.Devices())
	}
	queue := w.state.Queue()
	if len(queue) != 1 || queue[0].Ref != two || queue[0].Reason.Kind != core.WaitFull || queue[0].Reason.Resource != neuronPoolID {
		t.Fatalf("the queue is %+v, want the job of 2 waiting for the pool", queue)
	}
	end(holder(2))
	if g := w.holding[two]; g == nil || !g.Devices.IsRange() || g.Devices.Len() != 2 {
		t.Fatalf("once cores 1 to 3 were free the job of 2 was given %+v", g)
	}
}
