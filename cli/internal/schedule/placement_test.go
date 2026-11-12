package schedule_test

import (
	"fmt"
	"math/rand/v2"
	"reflect"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// randomDemand is a config of up to four placements, each giving a random set of resource kinds
// and claiming up to two resources besides its slot.
func randomDemand(r *rand.Rand) schedule.PlacementDemand {
	d := schedule.PlacementDemand{Placements: map[string]schedule.Offered{}, Host: "host"}
	for _, name := range []string{"p0", "p1", "p2", "p3"}[:r.IntN(5)] {
		var o schedule.Offered
		for _, k := range core.ResourceKinds() {
			if r.IntN(2) == 0 {
				o.Resources = append(o.Resources, k)
			}
		}
		for i := range r.IntN(3) {
			o.Claims = append(o.Claims, schedule.Claim{Resource: schedule.ResourceID(fmt.Sprintf("r%d", i)), Amount: r.IntN(3), Exclusive: r.IntN(4) == 0})
		}
		d.Placements[name] = o
	}
	return d
}

func randomKinds(r *rand.Rand) []core.ResourceKind {
	var out []core.ResourceKind
	for _, k := range core.ResourceKinds() {
		if r.IntN(3) == 0 {
			out = append(out, k)
		}
	}
	return out
}

// For any config and job: a queued docker job may run on exactly the placements that give every
// resource its task needs, or on the one it is restricted to when that one does, each once and by
// name; a placement that lacks a need is never one of its options. On each it claims a slot of the
// placement first and then what the placement's resources claim, and a measure claims every one of
// them whole. A docker job that started may run only where it started, and any other job claims a
// slot of the host.
func TestAJobMayRunOnEveryPlacementThatGivesWhatItsTaskNeeds(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	envs := []core.EnvironmentKind{core.EnvironmentDocker, core.EnvironmentDocker, core.EnvironmentHost}
	settings := []core.Settings{core.CheckSettings{}, core.MeasureSettings{}, core.CommandSettings{Image: core.ImageHarness, Argv: []string{"true"}}}
	for range 3000 {
		d := randomDemand(r)
		job := core.Job{Needs: randomKinds(r), Spec: core.JobSpec{
			Environment: envs[r.IntN(len(envs))], Placement: []string{"", "", "p0", "p2", "nope"}[r.IntN(5)], Settings: settings[r.IntN(len(settings))],
		}}
		started := r.IntN(4) == 0
		if started {
			job.Environment = &core.Fingerprint{Kind: job.Spec.Environment, Facts: map[string]string{core.FactPlacement: "p1"}}
		}
		measure := job.Spec.Settings.Kind() == core.JobMeasure
		got := d.Options(job)

		var want []string
		switch {
		case job.Spec.Environment != core.EnvironmentDocker:
			if !reflect.DeepEqual(got, schedule.Only(schedule.Claim{Resource: "host", Amount: 1, Exclusive: measure})) {
				t.Fatalf("%+v may run %+v, want a slot of the host", job.Spec, got)
			}
			continue
		case started:
			want = []string{"p1"}
		default:
			for name, o := range d.Placements {
				gives := !slices.ContainsFunc(job.Needs, func(k core.ResourceKind) bool { return !slices.Contains(o.Resources, k) })
				if gives && (job.Spec.Placement == "" || job.Spec.Placement == name) {
					want = append(want, name)
				}
			}
			slices.Sort(want)
		}
		var names []string
		for _, o := range got {
			names = append(names, o.Placement)
		}
		if !slices.Equal(names, want) {
			t.Fatalf("needs %v, restricted to %q, started %v, of %+v: may run on %v, want %v", job.Needs, job.Spec.Placement, started, d.Placements, names, want)
		}
		for _, o := range got {
			own := d.Placements[o.Placement].Claims
			if len(o.Claims) != 1+len(own) || o.Claims[0] != (schedule.Claim{Resource: schedule.PlacementOf(o.Placement), Amount: 1, Exclusive: measure}) {
				t.Fatalf("%+v claims %+v on %s, want a slot of it and %+v", job.Spec, o.Claims, o.Placement, own)
			}
			for i, c := range own {
				if want := (schedule.Claim{Resource: c.Resource, Amount: max(c.Amount, 1), Exclusive: c.Exclusive || measure}); o.Claims[1+i] != want {
					t.Fatalf("%+v claims %+v of %s on %s, want %+v", job.Spec, o.Claims[1+i], c.Resource, o.Placement, want)
				}
			}
		}
	}
}

// Placement names and what is held of the placements, in a step with a single job that may run on
// all of them.
type placed struct {
	names []string
	used  map[string]int
	slots map[string]int
}

func randomPlaced(r *rand.Rand) placed {
	p := placed{used: map[string]int{}, slots: map[string]int{}}
	for _, name := range []string{"pa", "pb", "pc", "pd"}[:1+r.IntN(4)] {
		p.names = append(p.names, name)
		p.slots[name] = 1 + r.IntN(4)
		p.used[name] = r.IntN(p.slots[name] + 1)
	}
	return p
}

// state is the step: each placement a resource with its slots, a holding per used slot, and one
// waiting job that may run on any of them, taking a slot.
func (p placed) state(r *rand.Rand) schedule.State {
	var s schedule.State
	w := schedule.Candidate{ID: "w"}
	for _, name := range p.names {
		s.Resources = append(s.Resources, schedule.Resource{ID: schedule.PlacementOf(name), Capacity: p.slots[name]})
		for i := range p.used[name] {
			s.Holdings = append(s.Holdings, schedule.Holding{ID: schedule.ID(fmt.Sprintf("%s-%d", name, i)), Claims: []schedule.Claim{{Resource: schedule.PlacementOf(name), Amount: 1}}})
		}
		w.Options = append(w.Options, schedule.Option{Placement: name, Claims: []schedule.Claim{{Resource: schedule.PlacementOf(name), Amount: 1}}})
	}
	r.Shuffle(len(w.Options), func(i, j int) { w.Options[i], w.Options[j] = w.Options[j], w.Options[i] })
	s.Waiting = []schedule.Candidate{w}
	return s
}

// A job starts on the least loaded placement it may run on: no placement with a free slot is less
// full, as a fraction of its slots, and of those as full the one whose name sorts first wins,
// whatever order the job lists them in. With every placement full it waits.
func TestAJobStartsOnTheLeastLoadedPlacementTiesByName(t *testing.T) {
	r := rand.New(rand.NewPCG(3, 4))
	for range 3000 {
		p := randomPlaced(r)
		var want string
		for _, name := range p.names {
			if p.used[name] == p.slots[name] {
				continue
			}
			if want == "" || p.used[name]*p.slots[want] < p.used[want]*p.slots[name] {
				want = name
			}
		}
		for range 3 {
			ds := schedule.Step(p.state(r), schedule.Rules{})
			st, started := ds[0].(schedule.Start)
			switch {
			case want == "" && started:
				t.Fatalf("%+v: started on %s with every placement full", p, st.Option.Placement)
			case want != "" && (!started || st.Option.Placement != want):
				t.Fatalf("%+v: decided %+v, want a start on %s", p, ds[0], want)
			}
		}
	}
}

// Two measures that may each run on either of two idle placements start at once, each on its own
// placement, which it holds alone; a third waits, keeping its turn on both, and names what holds
// the first placement it would take.
func TestMeasuresSpreadOverIdlePlacements(t *testing.T) {
	d := schedule.PlacementDemand{Placements: map[string]schedule.Offered{"a": {}, "b": {}}, Host: "host"}
	s := schedule.State{Resources: []schedule.Resource{{ID: schedule.PlacementOf("a"), Capacity: 2}, {ID: schedule.PlacementOf("b"), Capacity: 2}}}
	measure := core.Job{Spec: core.JobSpec{Environment: core.EnvironmentDocker, Settings: core.MeasureSettings{}}}
	for i := range 3 {
		s.Waiting = append(s.Waiting, schedule.Candidate{ID: schedule.ID(fmt.Sprintf("m%d", i+1)), Options: d.Options(measure), Seq: uint64(i)})
	}
	ds := schedule.Step(s, schedule.Rules{})
	on := map[schedule.ID]string{}
	for _, d := range ds {
		if st, ok := d.(schedule.Start); ok {
			on[st.ID] = st.Option.Placement
			if !st.Option.Claims[0].Exclusive {
				t.Fatalf("%s started on %s without holding it alone", st.ID, st.Option.Placement)
			}
		}
	}
	if want := map[schedule.ID]string{"m1": "a", "m2": "b"}; !reflect.DeepEqual(on, want) {
		t.Fatalf("started %v, want %v", on, want)
	}
	w, ok := ds[2].(schedule.Wait)
	if !ok || w.Reason.Kind != core.WaitHeldExclusively || !slices.Equal(w.Reason.Blockers, []schedule.ID{"m1"}) {
		t.Fatalf("the third measure: %+v, want it to wait for the placement m1 holds alone", ds[2])
	}
}

// On a placement that is Isolated, where each job's measured allocation is a node of its own, a
// measure claims a slot of it and its resources' claims as any job does, never whole, so measures
// fill its slots, while on a placement that is not, a measure holds it alone.
func TestAMeasureOfAnIsolatedPlacementTakesASlot(t *testing.T) {
	gpus := schedule.ResourceID("rocm:a")
	d := schedule.PlacementDemand{Placements: map[string]schedule.Offered{
		"a": {Isolated: true, Claims: []schedule.Claim{{Resource: gpus, Amount: 1}}},
		"b": {Claims: []schedule.Claim{{Resource: "rocm:b", Amount: 1}}},
	}, Host: "host"}
	s := schedule.State{Resources: []schedule.Resource{
		{ID: schedule.PlacementOf("a"), Capacity: 2}, {ID: gpus, Capacity: 2},
		{ID: schedule.PlacementOf("b"), Capacity: 2}, {ID: "rocm:b", Capacity: 2},
	}}
	for _, placement := range []string{"a", "b"} {
		measure := core.Job{Spec: core.JobSpec{Environment: core.EnvironmentDocker, Placement: placement, Settings: core.MeasureSettings{}}}
		options := d.Options(measure)
		if len(options) != 1 {
			t.Fatalf("a measure on %s may run on %+v", placement, options)
		}
		for _, c := range options[0].Claims {
			if c.Exclusive != (placement == "b") {
				t.Fatalf("a measure on %s claims %+v, want it whole only off an isolated placement", placement, c)
			}
		}
		for i := range 3 {
			s.Waiting = append(s.Waiting, schedule.Candidate{ID: schedule.ID(fmt.Sprintf("%s%d", placement, i+1)), Options: options, Seq: uint64(len(s.Waiting))})
		}
	}
	var started []schedule.ID
	for _, d := range schedule.Step(s, schedule.Rules{}) {
		if st, ok := d.(schedule.Start); ok {
			started = append(started, st.ID)
		}
	}
	if want := []schedule.ID{"a1", "a2", "b1"}; !slices.Equal(started, want) {
		t.Fatalf("started %v, want %v", started, want)
	}
}
