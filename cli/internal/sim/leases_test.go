package sim_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/memstores"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/fakelease"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// This simulation runs two users' daemons on one machine. They share nothing but the machine's
// lease: each has its own stores, commands and supervisors. Measures (commands that claim the
// machine alone) and commands that share it are submitted to both, commands end, daemons crash and
// start again, and virtual time passes for the retries. After every step it checks that two
// measures never run at once, but where the daemon of one of them died while it ran: a lease dies
// with its daemon, and the measure, whose supervisor outlives the daemon, goes on unleased until
// the next daemon adopts it, finds the lease taken, and reports the conflict. At the end every job
// has ended and nobody holds the lease.
//
// It is not the gated driver of the rest of this package: the daemons' own goroutines interleave
// as the Go scheduler runs them within each step, so a seed names the steps, not every
// interleaving. The checks hold for every interleaving.

// neighbor is one user's daemon and what outlives it.
type neighbor struct {
	user      string
	scope     core.Scope
	call      core.CallContext
	stores    *memstores.Stores
	env       *fakeenv.Environment
	checkouts *fakeenv.Checkouts
	survivors *supervisor.Survivors
	git       *gitrepo.Fake
	book      lease.Book
	app       *app.App
	// jobs are the jobs submitted, and alone those that claim the machine alone.
	jobs  []core.JobID
	alone map[string]bool
	// unleased are the measures that ran when this daemon died: they may meet another's.
	unleased map[string]bool
}

type neighborhood struct {
	rng       *rand.Rand
	gates     *fakeenv.MemoryGates
	place     *fakelease.Place
	daemons   []*neighbor
	n         int
	held      []string // the gates of commands that may be holding
	trace     []string
	overlaps  int
	refusals  int
	violation string
}

const neighborSlow = time.Second

func (h *neighborhood) start(d *neighbor) {
	d.app = app.New(app.Config{
		Clock: clock.System{}, Timers: clock.System{}, Stores: d.stores,
		Environments: fakeenv.Environments{core.EnvironmentHost: d.env},
		Supervisors:  supervisor.NewInProcess(fakeenv.Environments{core.EnvironmentHost: d.env}, d.checkouts, d.survivors),
		Repositories: d.git,
		Scheduler: app.Scheduler{
			Resources: []schedule.Resource{{ID: schedule.DefaultPlacement, Capacity: 2}},
			Demand:    aloneDemand{},
		},
		Leasing:    app.Leasing{Book: d.book, Holder: core.LeaseHolder{User: d.user, Host: "host-a", PID: 100 + len(d.user)}},
		SlowPeriod: neighborSlow,
	})
	if err := d.app.Start(context.Background()); err != nil {
		h.violation = fmt.Sprintf("%s's daemon did not start: %v", d.user, err)
	}
}

// aloneDemand gives a command whose first word starts with "alone" the machine to itself.
type aloneDemand struct{}

func (aloneDemand) Options(job core.Job) []schedule.Option {
	c, _ := job.Spec.Settings.(core.CommandSettings)
	alone := len(c.Argv) > 1 && strings.HasPrefix(c.Argv[1], "alone")
	return schedule.Only(schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1, Exclusive: alone})
}

func newNeighborhood(seed uint64, apart bool) *neighborhood {
	h := &neighborhood{rng: rand.New(rand.NewPCG(seed, 0x1ea5ed)), gates: fakeenv.NewMemoryGates(), place: &fakelease.Place{}}
	for _, user := range []string{"alice", "bob"} {
		scope, err := core.ResolvedScope(core.Repository("/home/"+user+"/repo"), "task")
		if err != nil {
			panic(err)
		}
		d := &neighbor{
			user: user, scope: scope,
			call:   core.CallContext{Scope: scope, Run: core.RunID("run-" + user), Harness: "h", Agent: "a"},
			stores: memstores.New(app.Rules(5)), env: fakeenv.New(core.EnvironmentHost, h.gates),
			checkouts: &fakeenv.Checkouts{}, survivors: supervisor.NewSurvivors(), git: gitrepo.NewFake(),
			book:  lease.Map{string(schedule.DefaultPlacement): {Leases: placeOf(h, apart).Daemon(), Key: "machine"}},
			alone: map[string]bool{}, unleased: map[string]bool{},
		}
		h.daemons = append(h.daemons, d)
		h.start(d)
	}
	return h
}

// placeOf is the place the daemons share, or with apart a place of each one's own: daemons that do
// not share their lease, which the simulation must catch.
func placeOf(h *neighborhood, apart bool) *fakelease.Place {
	if apart {
		return &fakelease.Place{}
	}
	return h.place
}

func (h *neighborhood) tracef(format string, args ...any) {
	h.trace = append(h.trace, fmt.Sprintf(format, args...))
}

func (h *neighborhood) submit(d *neighbor) {
	h.n++
	name := fmt.Sprintf("s%d", h.n)
	if h.rng.IntN(2) == 0 {
		name = fmt.Sprintf("alone%d", h.n)
		d.alone[name] = true
	}
	gate := "g-" + name
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	out, err := d.app.Submit(ctx, app.SubmitRequest{Context: d.call, Spec: core.JobSpec{
		Commit: commitOf(d), Environment: core.EnvironmentHost,
		Settings: core.CommandSettings{Image: core.ImageHarness, Argv: []string{"mark", name, ";", "hold", gate, ";", "exit", "0"}},
	}})
	if err != nil {
		h.violation = fmt.Sprintf("%s could not submit: %v", d.user, err)
		return
	}
	d.jobs = append(d.jobs, out.Job.ID)
	h.held = append(h.held, gate)
	h.tracef("%s submits %s (%s)", d.user, name, out.Job.ID)
}

func commitOf(*neighbor) core.Commit { return core.Commit(strings.Repeat("c", 40)) }

// check sees that measures of the two daemons run at once only where one's daemon died while it
// ran.
func (h *neighborhood) check() {
	var running []string
	owner := map[string]*neighbor{}
	for _, d := range h.daemons {
		for _, x := range d.env.ActiveRuns() {
			if len(x.Argv) > 1 && d.alone[x.Argv[1]] {
				running = append(running, x.Argv[1])
				owner[x.Argv[1]] = d
			}
		}
	}
	for i, a := range running {
		for _, b := range running[i+1:] {
			if owner[a] == owner[b] {
				h.violation = fmt.Sprintf("%s's measures %s and %s run at once", owner[a].user, a, b)
				return
			}
			if !owner[a].unleased[a] && !owner[b].unleased[b] {
				h.violation = fmt.Sprintf("the measures %s of %s and %s of %s run at once, and both daemons held their leases", a, owner[a].user, b, owner[b].user)
				return
			}
			h.overlaps++
		}
	}
}

// noteRefusals counts the measures that wait for a lease another daemon holds.
func (h *neighborhood) noteRefusals() {
	for _, d := range h.daemons {
		if d.app == nil {
			continue
		}
		for _, w := range d.app.Waits(d.scope, d.jobs) {
			if w.Kind == core.WaitLeased {
				h.refusals++
			}
		}
	}
}

func (h *neighborhood) step(i int) {
	d := h.daemons[h.rng.IntN(len(h.daemons))]
	switch op := h.rng.IntN(10); {
	case op < 3 && d.app != nil:
		h.submit(d)
	case op < 6 && len(h.held) > 0:
		k := h.rng.IntN(len(h.held))
		h.tracef("%d: %s ends", i, h.held[k])
		h.gates.Release(h.held[k])
		h.held = slices.Delete(h.held, k, k+1)
	case op < 7 && d.app != nil:
		// The daemon dies. Its measures go on, unleased, until the next one adopts them.
		for _, x := range d.env.ActiveRuns() {
			if len(x.Argv) > 1 && d.alone[x.Argv[1]] {
				d.unleased[x.Argv[1]] = true
			}
		}
		h.tracef("%d: %s's daemon dies", i, d.user)
		d.app.Close()
		d.app = nil
	case op < 8 && d.app == nil:
		h.tracef("%d: %s's daemon starts", i, d.user)
		h.start(d)
	default:
		clock.Sleep(context.Background(), clock.System{}, neighborSlow)
	}
}

// finish starts every daemon, ends every command, and waits until every job ended and nobody holds
// the lease.
func (h *neighborhood) finish() {
	for _, d := range h.daemons {
		if d.app == nil {
			h.start(d)
		}
	}
	for _, g := range h.held {
		h.gates.Release(g)
	}
	for range 600 {
		synctest.Wait()
		h.check()
		if h.violation != "" {
			return
		}
		if h.ended() && len(h.place.Held()) == 0 {
			return
		}
		clock.Sleep(context.Background(), clock.System{}, neighborSlow)
	}
	h.violation = fmt.Sprintf("work is left after the end: leases %v", h.place.Held())
}

func (h *neighborhood) ended() bool {
	ctx := context.Background()
	for _, d := range h.daemons {
		for _, id := range d.jobs {
			j, err := d.app.Job(ctx, d.scope, id)
			if err != nil || !j.State.Final() {
				return false
			}
		}
	}
	return true
}

func (h *neighborhood) teardown() {
	for _, d := range h.daemons {
		if d.app != nil {
			d.app.Close()
		}
		d.survivors.Close()
	}
}

func simulateNeighbors(seed uint64, apart bool) (violation string, h *neighborhood) {
	h = newNeighborhood(seed, apart)
	defer h.teardown()
	for i := range 80 {
		synctest.Wait()
		h.check()
		h.noteRefusals()
		if h.violation != "" {
			break
		}
		h.step(i)
	}
	if h.violation == "" {
		h.finish()
	}
	return h.violation, h
}

func TestSeedsOfTwoDaemonsSharingALeaseHoldTheInvariants(t *testing.T) {
	t.Parallel()
	first, count := seedRange()
	count = min(count, defaultSeeds/3)
	overlaps, refusals := 0, 0
	for seed := first; seed < first+count; seed++ {
		var violation string
		var h *neighborhood
		synctest.Test(t, func(*testing.T) { violation, h = simulateNeighbors(seed, false) })
		overlaps, refusals = overlaps+h.overlaps, refusals+h.refusals
		if violation != "" {
			t.Errorf("seed %d: %s\n%s", seed, violation, strings.Join(h.trace, "\n"))
		}
	}
	t.Logf("in %d seeds, measures waited for the other daemon's lease %d times, and met across a daemon's death %d times", count, refusals, overlaps)
	if count == defaultSeeds/3 && refusals == 0 {
		t.Errorf("no measure ever waited for the other daemon's lease")
	}
}

// TestTheNeighborSimulationCatchesDaemonsThatShareNoLease runs the same seeds with each daemon
// leasing the machine at a place of its own, as two daemons that do not share their leases would,
// and sees two measures run at once.
func TestTheNeighborSimulationCatchesDaemonsThatShareNoLease(t *testing.T) {
	first, _ := seedRange()
	for seed := first; seed < first+40; seed++ {
		var violation string
		synctest.Test(t, func(*testing.T) { violation, _ = simulateNeighbors(seed, true) })
		if strings.Contains(violation, "both daemons held their leases") {
			t.Logf("seed %d: %s", seed, violation)
			return
		}
	}
	t.Fatalf("no seed caught two daemons that share no lease")
}
