// Package scheduletest is the contract every schedule.Policy and schedule.Placer passes: properties
// of schedule.Step with the policy and the placer, over generated states. Each policy's package
// runs it on itself.
package scheduletest

import (
	"fmt"
	"math/rand/v2"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// Contract is what a policy is held to.
type Contract struct {
	// New returns the policy under test.
	New func() schedule.Policy
	// Fair says the policy shares resources between runs: a run that keeps work waiting gets a
	// start within one turn of every other run, whatever the others submit.
	Fair bool
	// Placer returns the placer under test. It defaults to schedule.LeastLoaded.
	Placer func() schedule.Placer
	// Step is the scheduler the policy runs in. It defaults to schedule.Step, and a test of the
	// contract itself passes a broken one to see the contract catch it.
	Step func(schedule.State, schedule.Rules) []schedule.Decision
}

// rules are the contract's policy and placer, new.
func (c Contract) rules() schedule.Rules {
	return schedule.Rules{Policy: c.New(), Placer: c.Placer()}
}

// Cases is how many generated states each property is checked on.
const Cases = 3000

// TB is the part of testing.TB the properties use.
type TB interface {
	Helper()
	Fatalf(format string, args ...any)
	Failed() bool
}

// properties are the contract's properties, by name.
func properties(c Contract) map[string]func(TB, Contract) {
	out := map[string]func(TB, Contract){
		"the policy orders the jobs it is given, each once":                orderIsPermutation,
		"no resource is given past its capacity":                           neverOverCapacity,
		"an exclusive claim shares its resource with no other":             exclusiveIsAlone,
		"every waiting job starts, waits for a true reason or is refused":  everyJobDecided,
		"the decisions are a function of the state":                        deterministic,
		"every job starts once the holders end and nothing more arrives":   eventuallyStarts,
		"a job that waits for a resource keeps its turn on it":             keepsTurn,
		"a job refused a lease waits for it, naming its holder":            leasedWaits,
		"a job starts on one of its options, and never one it cannot hold": startsOnItsOwnOption,
	}
	if c.Fair {
		out["a run with work waiting starts within one turn of every run"] = boundedStarvation
	}
	return out
}

func (c Contract) withDefaults() Contract {
	if c.Step == nil {
		c.Step = schedule.Step
	}
	if c.Placer == nil {
		c.Placer = func() schedule.Placer { return schedule.LeastLoaded{} }
	}
	return c
}

// Run checks the contract.
func Run(t *testing.T, c Contract) {
	t.Helper()
	c = c.withDefaults()
	for name, property := range properties(c) {
		t.Run(name, func(t *testing.T) { property(t, c) })
	}
}

// Broken returns the first violation of each property the contract finds, by property, and none
// for a policy and a scheduler that keep it. A test of the contract itself uses it to see the
// contract catch a broken scheduler.
func Broken(c Contract) map[string]string {
	c = c.withDefaults()
	out := map[string]string{}
	for name, property := range properties(c) {
		r := &recorder{}
		func() {
			defer func() {
				if v := recover(); v != nil && v != errStop {
					r.msg = fmt.Sprint("panic: ", v)
				}
			}()
			property(r, c)
		}()
		if r.msg != "" {
			out[name] = r.msg
		}
	}
	return out
}

var errStop = &struct{ msg string }{"stop"}

// recorder is a TB that keeps the first failure and stops the property there.
type recorder struct{ msg string }

func (*recorder) Helper() {}

func (r *recorder) Fatalf(format string, args ...any) {
	r.msg = fmt.Sprintf(format, args...)
	panic(errStop)
}

func (r *recorder) Failed() bool { return r.msg != "" }

// gen makes states from a seed: a few resources of small capacity, holdings that mostly fit and
// sometimes do not (a daemon restarted with less capacity adopts what runs), and waiting jobs with
// ties in time, unknown resources, claims past capacity, exclusive claims and First jobs, each of
// which may run on one to three placements, or rarely on none.
type gen struct{ r *rand.Rand }

func newGen(seed uint64) gen { return gen{rand.New(rand.NewPCG(seed, 0x5eed))} }

var (
	epoch = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	runs  = []core.RunID{"", "run-a", "run-b", "run-c"}
)

func (g gen) resources() []schedule.Resource {
	n := 1 + g.r.IntN(3)
	out := make([]schedule.Resource, n)
	for i := range out {
		out[i] = schedule.Resource{ID: schedule.ResourceID(fmt.Sprintf("r%d", i)), Capacity: 1 + g.r.IntN(4)}
	}
	return out
}

func (g gen) claims(rs []schedule.Resource) []schedule.Claim {
	n := 1 + g.r.IntN(2)
	out := make([]schedule.Claim, 0, n)
	for range n {
		var id schedule.ResourceID
		capacity := 1
		if g.r.IntN(25) == 0 {
			id = "unknown"
		} else {
			r := rs[g.r.IntN(len(rs))]
			id, capacity = r.ID, r.Capacity
		}
		amount := 1
		if g.r.IntN(4) == 0 {
			amount = 1 + g.r.IntN(capacity+1)
		}
		out = append(out, schedule.Claim{Resource: id, Amount: amount, Exclusive: g.r.IntN(4) == 0})
	}
	return out
}

func (g gen) state() schedule.State {
	s := schedule.State{Resources: g.resources()}
	for i := range g.r.IntN(4) {
		s.Holdings = append(s.Holdings, schedule.Holding{
			ID: schedule.ID(fmt.Sprintf("h%d", i)), Claims: g.claims(s.Resources), Run: runs[g.r.IntN(len(runs))],
		})
	}
	if g.r.IntN(3) != 0 {
		// Mostly holdings that fit, as the scheduler made them.
		s.Holdings = fitting(s.Resources, s.Holdings)
	}
	for i := range g.r.IntN(9) {
		s.Waiting = append(s.Waiting, g.candidate(s.Resources, i))
	}
	for range g.r.IntN(schedule.RecentGrants + 4) {
		s.Recent = schedule.Remember(s.Recent, schedule.Grant{Run: runs[g.r.IntN(len(runs))], Claims: g.claims(s.Resources)})
	}
	return s
}

// placements are the names generated options run on.
var placements = []string{"", "pa", "pb", "pc"}

func (g gen) candidate(rs []schedule.Resource, i int) schedule.Candidate {
	c := schedule.Candidate{
		ID: schedule.ID(fmt.Sprintf("w%d", i)), Run: runs[g.r.IntN(len(runs))],
		Submitted: epoch.Add(time.Duration(g.r.IntN(4)) * time.Second), Seq: uint64(100 + i), First: g.r.IntN(8) == 0,
	}
	n := 1
	switch g.r.IntN(12) {
	case 0:
		n = 0
	case 1, 2, 3, 4:
		n = 2 + g.r.IntN(2)
	}
	for _, at := range g.r.Perm(len(placements))[:n] {
		c.Options = append(c.Options, schedule.Option{Placement: placements[at], Claims: g.claims(rs)})
	}
	// Some were refused the lease of a claim by another daemon.
	if n > 0 && g.r.IntN(10) == 0 {
		o := c.Options[g.r.IntN(n)]
		c.Leased = &schedule.LeasedOut{Resource: o.Claims[0].Resource, Holder: core.LeaseHolder{User: "other", Host: "host-b", PID: 7}}
	}
	return c
}

// grantable says whether the claims could ever be granted.
func grantable(rs []schedule.Resource, claims []schedule.Claim) bool {
	capacity := map[schedule.ResourceID]int{}
	for _, r := range rs {
		capacity[r.ID] = r.Capacity
	}
	total := map[schedule.ResourceID]int{}
	for _, cl := range claims {
		total[cl.Resource] += max(cl.Amount, 1)
	}
	for r, n := range total {
		if cp, ok := capacity[r]; !ok || n > cp {
			return false
		}
	}
	return true
}

// fitting keeps the holdings that fit beside those before them.
func fitting(rs []schedule.Resource, hs []schedule.Holding) []schedule.Holding {
	var out []schedule.Holding
	for _, h := range hs {
		if len(violations(rs, append(slices.Clone(out), h))) == 0 {
			out = append(out, h)
		}
	}
	return out
}

// violations lists how a set of holdings breaks the two rules.
func violations(rs []schedule.Resource, hs []schedule.Holding) []string {
	capacity := map[schedule.ResourceID]int{}
	for _, r := range rs {
		capacity[r.ID] = r.Capacity
	}
	used := map[schedule.ResourceID]int{}
	holders := map[schedule.ResourceID][]schedule.ID{}
	alone := map[schedule.ResourceID]bool{}
	for _, h := range hs {
		seen := map[schedule.ResourceID]bool{}
		for _, c := range h.Claims {
			used[c.Resource] += max(c.Amount, 1)
			if !seen[c.Resource] {
				holders[c.Resource] = append(holders[c.Resource], h.ID)
				seen[c.Resource] = true
			}
			alone[c.Resource] = alone[c.Resource] || c.Exclusive
		}
	}
	var out []string
	for r, n := range used {
		if c, ok := capacity[r]; !ok || n > c {
			out = append(out, fmt.Sprintf("%s holds %d of capacity %d", r, n, c))
		}
		if alone[r] && len(holders[r]) > 1 {
			out = append(out, fmt.Sprintf("%s is held exclusively and shared by %v", r, holders[r]))
		}
	}
	slices.Sort(out)
	return out
}

func each(t TB, check func(t TB, seed uint64, s schedule.State)) {
	t.Helper()
	for seed := range uint64(Cases) {
		check(t, seed, newGen(seed).state())
		if t.Failed() {
			return
		}
	}
}

// started returns the waiting jobs Step started, as holdings.
func started(s schedule.State, ds []schedule.Decision) []schedule.Holding {
	var out []schedule.Holding
	for _, d := range ds {
		if st, ok := d.(schedule.Start); ok {
			i := slices.IndexFunc(s.Waiting, func(c schedule.Candidate) bool { return c.ID == st.ID })
			out = append(out, schedule.Holding{ID: st.ID, Claims: st.Option.Claims, Run: s.Waiting[i].Run})
		}
	}
	return out
}

func orderIsPermutation(t TB, c Contract) {
	each(t, func(t TB, seed uint64, s schedule.State) {
		in := schedule.ArrivalOrder(s.Waiting)
		out := c.New().Order(slices.Clone(in), schedule.UsageOf(s))
		ids := func(cs []schedule.Candidate) []schedule.ID {
			out := make([]schedule.ID, len(cs))
			for i, c := range cs {
				out[i] = c.ID
			}
			slices.Sort(out)
			return out
		}
		if !slices.Equal(ids(in), ids(out)) {
			t.Fatalf("seed %d: ordered %v from %v", seed, ids(out), ids(in))
		}
	})
}

// newViolations is what the started jobs broke that the holdings had not broken already.
func newViolations(s schedule.State, ds []schedule.Decision) []string {
	before := violations(s.Resources, s.Holdings)
	var out []string
	for _, v := range violations(s.Resources, append(slices.Clone(s.Holdings), started(s, ds)...)) {
		if !slices.Contains(before, v) {
			out = append(out, v)
		}
	}
	return out
}

func neverOverCapacity(t TB, c Contract) {
	each(t, func(t TB, seed uint64, s schedule.State) {
		for _, v := range newViolations(s, c.Step(s, c.rules())) {
			if !containsWord(v, "exclusively") {
				t.Fatalf("seed %d: %s", seed, v)
			}
		}
	})
}

func exclusiveIsAlone(t TB, c Contract) {
	each(t, func(t TB, seed uint64, s schedule.State) {
		for _, v := range newViolations(s, c.Step(s, c.rules())) {
			if containsWord(v, "exclusively") {
				t.Fatalf("seed %d: %s", seed, v)
			}
		}
		// A resource held exclusively and shared already is given to no one more.
		ds := c.Step(s, c.rules())
		for _, h := range started(s, ds) {
			for _, claim := range h.Claims {
				for _, other := range s.Holdings {
					for _, oc := range other.Claims {
						if oc.Resource == claim.Resource && (oc.Exclusive || claim.Exclusive) {
							t.Fatalf("seed %d: %s started on %s beside %s", seed, h.ID, claim.Resource, other.ID)
						}
					}
				}
			}
		}
	})
}

func containsWord(s, w string) bool {
	for i := 0; i+len(w) <= len(s); i++ {
		if s[i:i+len(w)] == w {
			return true
		}
	}
	return false
}

func everyJobDecided(t TB, c Contract) {
	each(t, func(t TB, seed uint64, s schedule.State) {
		ds := c.Step(s, c.rules())
		seen := map[schedule.ID]int{}
		for _, d := range ds {
			seen[d.Target()]++
		}
		for _, w := range s.Waiting {
			if seen[w.ID] != 1 {
				t.Fatalf("seed %d: job %s got %d decisions", seed, w.ID, seen[w.ID])
			}
		}
		if len(ds) != len(s.Waiting) {
			t.Fatalf("seed %d: %d decisions for %d jobs", seed, len(ds), len(s.Waiting))
		}
		capacity := map[schedule.ResourceID]int{}
		for _, r := range s.Resources {
			capacity[r.ID] = r.Capacity
		}
		known := map[schedule.ID]bool{}
		for _, h := range s.Holdings {
			known[h.ID] = true
		}
		for _, w := range s.Waiting {
			known[w.ID] = true
		}
		for _, d := range ds {
			i := slices.IndexFunc(s.Waiting, func(c schedule.Candidate) bool { return c.ID == d.Target() })
			cand := s.Waiting[i]
			possible := slices.ContainsFunc(cand.Options, func(o schedule.Option) bool { return grantable(s.Resources, o.Claims) })
			switch d := d.(type) {
			case schedule.Refuse:
				if possible || d.Detail == "" {
					t.Fatalf("seed %d: refused %s, whose options %v fit %v, saying %q", seed, d.ID, cand.Options, s.Resources, d.Detail)
				}
			case schedule.Wait:
				if !possible {
					t.Fatalf("seed %d: %s waits for options %v that can never be granted", seed, d.ID, cand.Options)
				}
				checkReason(t, seed, d, known)
			case schedule.Start:
				if !grantable(s.Resources, d.Option.Claims) {
					t.Fatalf("seed %d: started %s on %v, which exceeds %v", seed, d.ID, d.Option, s.Resources)
				}
			}
		}
	})
}

// startsOnItsOwnOption: a job starts on one of the options it has, as it has it, and only on one
// whose claims fit; with a placer that ranks them in any order, safety holds.
func startsOnItsOwnOption(t TB, c Contract) {
	each(t, func(t TB, seed uint64, s schedule.State) {
		for _, d := range c.Step(s, c.rules()) {
			st, ok := d.(schedule.Start)
			if !ok {
				continue
			}
			i := slices.IndexFunc(s.Waiting, func(c schedule.Candidate) bool { return c.ID == st.ID })
			if !slices.ContainsFunc(s.Waiting[i].Options, func(o schedule.Option) bool { return reflect.DeepEqual(o, st.Option) }) {
				t.Fatalf("seed %d: %s started on %+v, which is none of its options %+v", seed, st.ID, st.Option, s.Waiting[i].Options)
			}
		}
	})
}

func checkReason(t TB, seed uint64, d schedule.Wait, known map[schedule.ID]bool) {
	t.Helper()
	r := d.Reason
	if !slices.Contains(core.WaitKinds(), r.Kind) || r.Kind == core.WaitTrouble {
		t.Fatalf("seed %d: %s waits for %q, which is not a reason the scheduler gives", seed, d.ID, r.Kind)
	}
	if r.Kind == core.WaitLeased {
		if r.Resource == "" || r.Elsewhere == nil {
			t.Fatalf("seed %d: %s waits for a lease with no resource or holder: %+v", seed, d.ID, r)
		}
		return
	}
	if r.Kind == core.WaitHeld {
		if r.Detail == "" {
			t.Fatalf("seed %d: %s is held with no reason", seed, d.ID)
		}
		return
	}
	if r.Resource == "" || len(r.Blockers) == 0 {
		t.Fatalf("seed %d: %s waits for %q with no resource or blocker: %+v", seed, d.ID, r.Kind, r)
	}
	for _, b := range r.Blockers {
		if !known[b] || b == d.ID {
			t.Fatalf("seed %d: %s waits on %s, which is no other job", seed, d.ID, b)
		}
	}
}

func deterministic(t TB, c Contract) {
	each(t, func(t TB, seed uint64, s schedule.State) {
		first := c.Step(s, c.rules())
		again := c.Step(s, c.rules())
		shuffled := s
		shuffled.Waiting = slices.Clone(s.Waiting)
		r := rand.New(rand.NewPCG(seed, 1))
		r.Shuffle(len(shuffled.Waiting), func(i, j int) { shuffled.Waiting[i], shuffled.Waiting[j] = shuffled.Waiting[j], shuffled.Waiting[i] })
		other := c.Step(shuffled, c.rules())
		if !reflect.DeepEqual(first, again) || !reflect.DeepEqual(first, other) {
			t.Fatalf("seed %d: the same state decided\n%+v\n%+v\n%+v", seed, first, again, other)
		}
	})
}

// eventuallyStarts runs the jobs of a state to the end: each round starts what Step starts and ends
// some of what runs. Every job that is not refused starts, within a round per job.
func eventuallyStarts(t TB, c Contract) {
	each(t, func(t TB, seed uint64, s schedule.State) {
		r := rand.New(rand.NewPCG(seed, 2))
		s.Holdings = fitting(s.Resources, s.Holdings)
		waiting := len(s.Waiting)
		for round := 0; len(s.Waiting) > 0; round++ {
			if round > 0 {
				// The other daemons give back the leases they held.
				for i := range s.Waiting {
					s.Waiting[i].Leased = nil
				}
			}
			if round > 2*(waiting+len(s.Holdings))+2 {
				t.Fatalf("seed %d: %d jobs still wait after %d rounds: %+v", seed, len(s.Waiting), round, c.Step(s, c.rules()))
			}
			ds := c.Step(s, c.rules())
			began := len(started(s, ds))
			for _, d := range ds {
				switch d := d.(type) {
				case schedule.Start, schedule.Refuse:
					i := slices.IndexFunc(s.Waiting, func(c schedule.Candidate) bool { return c.ID == d.Target() })
					if _, ok := d.(schedule.Start); ok {
						claims := d.(schedule.Start).Option.Claims
						s.Holdings = append(s.Holdings, schedule.Holding{ID: d.Target(), Claims: claims, Run: s.Waiting[i].Run})
						s.Recent = schedule.Remember(s.Recent, schedule.Grant{Run: s.Waiting[i].Run, Claims: claims})
					}
					s.Waiting = slices.Delete(s.Waiting, i, i+1)
				case schedule.Wait:
					// It is considered again next round.
				}
			}
			// Some of what runs ends, and with nothing started, all of it.
			if began == 0 {
				s.Holdings = nil
			} else {
				s.Holdings = slices.DeleteFunc(s.Holdings, func(schedule.Holding) bool { return r.IntN(2) == 0 })
			}
		}
	})
}

// boundedStarvation keeps every run's queue full on one resource of one slot, whatever the runs
// submit: one run floods its queue with old jobs, and the others trickle. Each round the job that
// started ends. Every run must get a start within as many rounds as there are runs.
func boundedStarvation(t TB, c Contract) {
	for seed := range uint64(Cases / 30) {
		r := rand.New(rand.NewPCG(seed, 3))
		nRuns := 2 + r.IntN(3)
		s := schedule.State{Resources: []schedule.Resource{{ID: schedule.DefaultPlacement, Capacity: 1}}}
		seq := uint64(0)
		add := func(run core.RunID, at time.Time) {
			seq++
			s.Waiting = append(s.Waiting, schedule.Candidate{
				ID: schedule.ID(fmt.Sprintf("j%d", seq)), Run: run, Submitted: at, Seq: seq,
				Options: schedule.Only(schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1, Exclusive: r.IntN(3) == 0}),
			})
		}
		// The flooding run submitted a long queue first.
		for range 50 {
			add(runs[1], epoch)
		}
		last := map[core.RunID]int{}
		for round := 1; round <= 120; round++ {
			now := epoch.Add(time.Duration(round) * time.Second)
			for i := range nRuns {
				run := runs[1+i%3]
				if i == 3 {
					run = runs[0]
				}
				if !slices.ContainsFunc(s.Waiting, func(c schedule.Candidate) bool { return c.Run == run }) {
					add(run, now)
				}
				if last[run] == 0 {
					last[run] = round
				}
			}
			ds := c.Step(s, c.rules())
			s.Holdings = nil
			for _, h := range started(s, ds) {
				s.Recent = schedule.Remember(s.Recent, schedule.Grant{Run: h.Run, Claims: h.Claims})
				last[h.Run] = round
				s.Waiting = slices.DeleteFunc(s.Waiting, func(c schedule.Candidate) bool { return c.ID == h.ID })
			}
			for run, at := range last {
				if round-at > nRuns {
					t.Fatalf("seed %d: run %q has had no start since round %d, at round %d", seed, run, at, round)
				}
			}
		}
	}
}

// keepsTurn: once a job waits for a resource, no job considered after it in the same step starts on
// that resource, so a stream of small jobs cannot starve a large or exclusive one.
func leasedWaits(t TB, c Contract) {
	each(t, func(t TB, seed uint64, s schedule.State) {
		for _, d := range c.Step(s, c.rules()) {
			i := slices.IndexFunc(s.Waiting, func(c schedule.Candidate) bool { return c.ID == d.Target() })
			l := s.Waiting[i].Leased
			if l == nil {
				continue
			}
			switch d := d.(type) {
			case schedule.Start:
				if slices.ContainsFunc(d.Option.Claims, func(cl schedule.Claim) bool { return cl.Resource == l.Resource }) {
					t.Fatalf("seed %d: %s started on %s while %s holds the lease of %s", seed, d.ID, d.Option.Placement, l.Holder, l.Resource)
				}
			case schedule.Wait:
				if d.Reason.Kind == core.WaitLeased && (d.Reason.Elsewhere == nil || *d.Reason.Elsewhere != l.Holder) {
					t.Fatalf("seed %d: %s waits for the lease of %s and names %v, want %s", seed, d.ID, l.Resource, d.Reason.Elsewhere, l.Holder)
				}
			case schedule.Refuse:
			}
		}
	})
}

func keepsTurn(t TB, c Contract) {
	each(t, func(t TB, seed uint64, s schedule.State) {
		// A job waits on every option it could ever hold.
		claimsOf := func(id schedule.ID) []schedule.Claim {
			i := slices.IndexFunc(s.Waiting, func(c schedule.Candidate) bool { return c.ID == id })
			var out []schedule.Claim
			for _, o := range s.Waiting[i].Options {
				if grantable(s.Resources, o.Claims) {
					out = append(out, o.Claims...)
				}
			}
			return out
		}
		reserved := map[schedule.ResourceID]schedule.ID{}
		for _, d := range c.Step(s, c.rules()) {
			switch d := d.(type) {
			case schedule.Wait:
				if d.Reason.Kind == core.WaitHeld {
					continue
				}
				for _, cl := range claimsOf(d.ID) {
					if _, ok := reserved[cl.Resource]; !ok {
						reserved[cl.Resource] = d.ID
					}
				}
			case schedule.Start:
				for _, cl := range d.Option.Claims {
					if w, ok := reserved[cl.Resource]; ok {
						t.Fatalf("seed %d: %s started on %s, which %s waits for", seed, d.ID, cl.Resource, w)
					}
				}
			case schedule.Refuse:
				// It never runs, so it keeps no turn.
			}
		}
	})
}
