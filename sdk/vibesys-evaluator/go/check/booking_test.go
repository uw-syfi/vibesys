package check_test

import (
	"encoding/json"
	"fmt"
	"iter"
	"math"
	"math/rand"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/check"
)

// A booking service, the shape of a reservation app: each resource has a capacity and units
// taken per slot (reserved strictly: decided against the exact count), reads of the units free
// and of the resource's name that may be up to 1 s stale, capacity changes that must not drop
// below what is taken, and restarts and cache flushes that lose nothing. The model uses all the
// helpers: Register (capacity), Counter in a PMap (units taken per slot), Window (the name).

const bound = time.Second

type bkIn struct {
	Kind  string `json:"kind"` // reserve, free, set_capacity, rename, name
	Res   int    `json:"res"`
	Slot  string `json:"slot,omitempty"`
	Units int    `json:"units,omitempty"` // reserve: units; set_capacity: the capacity
	Name  string `json:"name,omitempty"`  // rename
}

type bkOut struct {
	OK   bool   `json:"ok"`   // reserve: taken (else full); set_capacity: accepted (else conflict)
	Free int    `json:"free"` // free
	Name string `json:"name"` // name
}

type bkState struct {
	capacity check.Register[int]
	taken    check.PMap[check.Counter]
	name     check.Window[string]
}

func initialName(res int) string { return fmt.Sprintf("resource %d", res) }

func bkSpec(capacity int) *check.Spec[bkState, bkIn, bkOut] {
	return &check.Spec[bkState, bkIn, bkOut]{
		Name:         "booking",
		Project:      bkProject,
		EffectWindow: time.Second,
		Init: func(keys []string) bkState {
			var res int
			fmt.Sscanf(keys[0], "r%d", &res)
			return bkState{capacity: check.NewRegister(capacity), name: check.NewWindow(initialName(res))}
		},
		Step: bkStep,
		Equal: func(a, b bkState) bool {
			return a.capacity.Equal(b.capacity) && a.taken.Equal(b.taken) && a.name.Equal(b.name)
		},
		Hash: func(s bkState) uint64 { return s.capacity.Hash()*31 ^ s.taken.Hash()*17 ^ s.name.Hash() },
		DescribeState: func(s bkState) string {
			var slots []string
			s.taken.Range(func(k string, c check.Counter) { slots = append(slots, fmt.Sprintf("%s=%d", k, c.Value())) })
			return fmt.Sprintf("capacity %d, taken %v, names %q", s.capacity.Latest(), slots, s.name.Allowed(math.MinInt64, 0))
		},
	}
}

func bkStep(s bkState, op check.Op[bkIn, bkOut]) ([]bkState, string) {
	s.capacity = s.capacity.Prune(op.Floor, bound)
	s.taken = s.taken.MapRecent(func(c check.Counter) check.Counter { return c.Prune(op.Floor, bound) })
	s.name = s.name.Prune(op.Floor, bound)
	in, out := op.In, op.Out
	switch in.Kind {
	case "reserve":
		slot, _ := s.taken.Get(in.Slot)
		fits := slot.Value()+in.Units <= s.capacity.Latest()
		took := s
		took.taken = s.taken.Set(in.Slot, slot.Add(in.Units, op.Call, op.Return))
		switch {
		case op.Unknown && fits:
			return []bkState{s, took}, ""
		case op.Unknown:
			return []bkState{s}, ""
		case out.OK != fits:
			return nil, fmt.Sprintf("reserving %d in %s with %d of %d taken must answer ok=%v", in.Units, in.Slot, slot.Value(), s.capacity.Latest(), fits)
		case fits:
			return []bkState{took}, ""
		}
		return []bkState{s}, ""
	case "set_capacity":
		most := 0
		s.taken.Range(func(_ string, c check.Counter) { most = max(most, c.Value()) })
		ok := in.Units >= most
		set := s
		set.capacity = s.capacity.Write(in.Units, op.Return)
		switch {
		case op.Unknown && ok:
			return []bkState{s, set}, ""
		case op.Unknown:
			return []bkState{s}, ""
		case out.OK != ok:
			return nil, fmt.Sprintf("capacity %d with %d taken must answer ok=%v", in.Units, most, ok)
		case ok:
			return []bkState{set}, ""
		}
		return []bkState{s}, ""
	case "free":
		slot, _ := s.taken.Get(in.Slot)
		lo, hi := slot.Range(op.Call, bound)
		for _, c := range s.capacity.Allowed(op.Call, bound) {
			if c-hi <= out.Free && out.Free <= c-lo {
				return []bkState{s}, ""
			}
		}
		return nil, fmt.Sprintf("%s shows %d free; taken may be %d to %d of capacities %v", in.Slot, out.Free, lo, hi, s.capacity.Allowed(op.Call, bound))
	case "rename":
		n := s
		n.name = s.name.Write(in.Name, op.Call, op.Return)
		if op.Unknown {
			return []bkState{s, n}, ""
		}
		return []bkState{n}, ""
	case "name":
		if s.name.MayShow(out.Name, op.Call, bound) {
			return []bkState{s}, ""
		}
		return nil, fmt.Sprintf("shows name %q; it may show %q", out.Name, s.name.Allowed(op.Call, bound))
	}
	return nil, "unknown kind " + in.Kind
}

// bkProject is a task's projection: decode each call's info and body (strictly: a malformed body
// is a violation), one piece per call, keyed by the resource.
func bkProject(phases iter.Seq[[]check.Call], out *check.Projection[bkIn, bkOut]) error {
	for calls := range phases {
		for i := range calls {
			c := &calls[i]
			var in bkIn
			if err := json.Unmarshal(c.Info, &in); err != nil {
				return fmt.Errorf("%s %d: info: %w", c.Op, c.Index, err)
			}
			var res bkOut
			lost := c.Lost()
			if !lost {
				if c.Steps[0].Status != 200 {
					out.Violation(c, "HTTP %d", c.Steps[0].Status)
					continue
				}
				if err := json.Unmarshal(c.Steps[0].Body, &res); err != nil {
					out.Violation(c, "body: %v", err)
					continue
				}
			}
			write := in.Kind == "reserve" || in.Kind == "set_capacity" || in.Kind == "rename"
			out.Add(check.Piece[bkIn, bkOut]{Keys: []string{fmt.Sprintf("r%d", in.Res)}, In: in, Out: res, Unknown: lost, Write: write, Origin: c})
		}
	}
	return nil
}

// The simulated service. It decides each op at a linearization point inside the op's window and
// answers reads from its state up to Lag before that point; the bugs break one rule each.

type bugs struct {
	Race            bool // a reservation decides on the state at its call, takes at its linearization
	LoseOnRestart   bool // a restart forgets the epoch's reservations
	StaleReads      bool // reads may be up to 3 s stale
	FullShowsFree   bool // a full slot shows one unit free
	FlushLosesNames bool // a cache flush reverts names to their initial value
}

type sim struct {
	Seed               int64
	Resources          int
	Capacity           int
	Clients            int
	OpsPerClient       int // per epoch
	Epochs             int
	MeanDur, MeanThink time.Duration
	Lag                time.Duration
	SlotLen            time.Duration
	Lost               float64 // share of ops whose response is lost (half of them took effect)
	Bugs               bugs
}

func defaultSim(seed int64) sim {
	return sim{Seed: seed, Resources: 4, Capacity: 4, Clients: 8, OpsPerClient: 40, Epochs: 3,
		MeanDur: 20 * time.Millisecond, MeanThink: 30 * time.Millisecond, Lag: 500 * time.Millisecond, SlotLen: 500 * time.Millisecond}
}

type version struct {
	at       int64
	capacity int
	taken    map[string]int
	name     string
}

type simOp struct {
	client          int
	in              bkIn
	call, lp, ret   int64
	lost, applied   bool
	out             bkOut
	index           uint64
	epoch, snapshot int
}

func (p sim) history() *check.History {
	rng := rand.New(rand.NewSource(p.Seed))
	ms := int64(time.Millisecond)
	versions := make([][]version, p.Resources)
	for i := range versions {
		versions[i] = []version{{at: math.MinInt64, capacity: p.Capacity, taken: map[string]int{}, name: initialName(i)}}
	}
	cur := func(res int) version { return versions[res][len(versions[res])-1] }
	at := func(res int, t int64) version {
		vs := versions[res]
		i := sort.Search(len(vs), func(i int) bool { return vs[i].at > t })
		return vs[i-1]
	}
	commit := func(res int, t int64, change func(v *version)) {
		v := cur(res)
		v.at = t
		v.taken = cloneMap(v.taken)
		change(&v)
		versions[res] = append(versions[res], v)
	}
	var phases [][]check.Call
	var faults []check.Fault
	t0 := int64(time.Hour) // any origin
	var index uint64
	for epoch := range p.Epochs {
		startTaken := make([]map[string]int, p.Resources)
		for res := range p.Resources {
			startTaken[res] = cloneMap(cur(res).taken)
		}
		var ops []*simOp
		end := t0
		for client := range p.Clients {
			t := t0
			for range p.OpsPerClient {
				call := t + rng.Int63n(2*int64(p.MeanThink))
				dur := 1 + rng.Int63n(2*int64(p.MeanDur))
				op := &simOp{client: client, call: call, lp: call + rng.Int63n(dur), ret: call + dur, epoch: epoch, index: index}
				index++
				res := rng.Intn(p.Resources)
				slot := fmt.Sprintf("s%d", (call-int64(time.Hour))/int64(p.SlotLen)+rng.Int63n(3))
				switch k := rng.Intn(100); {
				case k < 40:
					op.in = bkIn{Kind: "reserve", Res: res, Slot: slot, Units: 1 + rng.Intn(2)}
				case k < 70:
					op.in = bkIn{Kind: "free", Res: res, Slot: slot}
				case k < 85:
					op.in = bkIn{Kind: "name", Res: res}
				case k < 92:
					op.in = bkIn{Kind: "rename", Res: res, Name: fmt.Sprintf("name %d", op.index)}
				default:
					op.in = bkIn{Kind: "set_capacity", Res: res, Units: 2 + rng.Intn(2*p.Capacity)}
				}
				op.lost = rng.Float64() < p.Lost
				op.applied = !op.lost || rng.Intn(2) == 0
				ops = append(ops, op)
				t = op.ret
				end = max(end, op.ret)
			}
		}
		sort.SliceStable(ops, func(i, j int) bool { return ops[i].lp < ops[j].lp })
		for _, op := range ops {
			in := op.in
			lag := rng.Int63n(int64(p.Lag))
			if p.Bugs.StaleReads {
				lag = rng.Int63n(int64(3 * time.Second))
			}
			switch in.Kind {
			case "reserve":
				decide := cur(in.Res)
				if p.Bugs.Race {
					decide = at(in.Res, op.call)
				}
				op.out.OK = decide.taken[in.Slot]+in.Units <= decide.capacity
				if op.out.OK && op.applied {
					commit(in.Res, op.lp, func(v *version) { v.taken[in.Slot] += in.Units })
				}
			case "set_capacity":
				most := 0
				for _, n := range cur(in.Res).taken {
					most = max(most, n)
				}
				op.out.OK = in.Units >= most
				if op.out.OK && op.applied {
					commit(in.Res, op.lp, func(v *version) { v.capacity = in.Units })
				}
			case "rename":
				op.out.OK = true
				if op.applied {
					commit(in.Res, op.lp, func(v *version) { v.name = in.Name })
				}
			case "free":
				v := at(in.Res, op.lp-lag)
				op.out.Free = v.capacity - v.taken[in.Slot]
				if p.Bugs.FullShowsFree && op.out.Free <= 0 {
					op.out.Free = 1
				}
			case "name":
				op.out.Name = at(in.Res, op.lp-lag).name
			}
		}
		sort.SliceStable(ops, func(i, j int) bool { return ops[i].call < ops[j].call })
		calls := make([]check.Call, 0, len(ops))
		for _, op := range ops {
			info, _ := json.Marshal(op.in)
			body, _ := json.Marshal(op.out)
			step := check.Step{Start: op.call, End: op.ret, Status: 200, Body: body}
			if op.lost {
				step = check.Step{Start: op.call, End: op.ret, Error: "timeout"}
			}
			calls = append(calls, check.Call{Phase: epoch, Index: op.index, Client: op.client, Op: op.in.Kind, Info: info, Start: op.call, End: op.ret, Steps: []check.Step{step}})
		}
		phases = append(phases, calls)
		if epoch == p.Epochs-1 {
			break
		}
		f := check.Fault{Kind: "restart", Target: "service", Start: end + ms, End: end + 20*ms, AfterPhase: epoch}
		if epoch%2 == 1 {
			f.Kind = "flush"
		}
		for res := range p.Resources {
			switch {
			case f.Kind == "restart" && p.Bugs.LoseOnRestart:
				commit(res, f.Start, func(v *version) { v.taken = cloneMap(startTaken[res]) })
			case f.Kind == "flush" && p.Bugs.FlushLosesNames:
				commit(res, f.Start, func(v *version) { v.name = initialName(res) })
			}
		}
		faults = append(faults, f)
		t0 = f.End + ms
	}
	return check.NewHistory(phases, faults)
}

func cloneMap(m map[string]int) map[string]int {
	out := make(map[string]int, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

func bkCheck(p sim, o check.Options) *check.Report {
	return check.Check(p.history(), []check.AnySpec{bkSpec(p.Capacity)}, o)
}

// Correct concurrent histories pass, with restarts, flushes, and responses lost (half of those
// took effect).
func TestCorrectHistoriesPass(t *testing.T) {
	for seed := range int64(10) {
		p := defaultSim(seed)
		p.Lost = 0.05
		rep := bkCheck(p, check.Options{MaxLostShare: -1})
		if rep.Verdict != check.Pass {
			t.Fatalf("seed %d: %s\n%s", seed, rep.Verdict, rep.Text(10))
		}
		if seed == 0 {
			t.Logf("%s", rep.Reason)
		}
	}
}

// Each injected bug is caught on most seeds, with an explanation and a timeline.
func TestInjectedBugsAreCaught(t *testing.T) {
	cases := []struct {
		name string
		bugs bugs
		min  int // seeds of 10 that catch it (the seeds and the search are deterministic: exact)
	}{
		{"overbooking race", bugs{Race: true}, 9},
		{"reservations lost on restart", bugs{LoseOnRestart: true}, 10},
		{"reads staler than 1 s", bugs{StaleReads: true}, 10},
		{"a full slot shows a unit free", bugs{FullShowsFree: true}, 6}, // only reads 1 s after the slot filled can tell
		{"a flush loses names", bugs{FlushLosesNames: true}, 10},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			caught := 0
			for seed := range int64(10) {
				p := defaultSim(seed)
				p.Bugs = c.bugs
				rep := bkCheck(p, check.Options{})
				switch rep.Verdict {
				case check.Fail:
					caught++
					f := rep.Specs[0].Failures[0]
					if !strings.Contains(f.Explanation, "first violation") || f.Timeline == nil {
						t.Fatalf("seed %d: the failure is not explained:\n%s", seed, rep.Text(5))
					}
				case check.Unknown:
					t.Fatalf("seed %d: UNKNOWN\n%s", seed, rep.Text(5))
				}
			}
			t.Logf("caught in %d of 10 seeds", caught)
			if caught < c.min {
				t.Fatalf("caught in %d of 10 seeds, want at least %d", caught, c.min)
			}
		})
	}
}

// An unknown outcome takes effect within EffectWindow of its call, or never: a lost reservation
// that reads show taking effect long after its call fails with a window and passes without one.
func TestEffectWindowBoundsUnknownOutcomes(t *testing.T) {
	ms := int64(time.Millisecond)
	call := func(i uint64, start int64, in bkIn, out bkOut, lost bool) check.Call {
		info, _ := json.Marshal(in)
		body, _ := json.Marshal(out)
		step := check.Step{Start: start, End: start + 10*ms, Status: 200, Body: body}
		if lost {
			step = check.Step{Start: start, End: start + 10*ms, Error: "timeout"}
		}
		return check.Call{Index: i, Client: -1, Op: in.Kind, Info: info, Start: start, End: start + 10*ms, Steps: []check.Step{step}}
	}
	h := one(
		call(0, 0, bkIn{Kind: "reserve", Slot: "s", Units: 4}, bkOut{}, true),
		call(1, 3000*ms, bkIn{Kind: "free", Slot: "s"}, bkOut{Free: 4}, false),  // not taken yet
		call(2, 10000*ms, bkIn{Kind: "free", Slot: "s"}, bkOut{Free: 0}, false), // taken now
	)
	for _, c := range []struct {
		window time.Duration
		want   check.Verdict
	}{{time.Second, check.Fail}, {0, check.Pass}} {
		s := bkSpec(4)
		s.EffectWindow = c.window
		if rep := check.Check(h, []check.AnySpec{s}, check.Options{MaxLostShare: -1}); rep.Verdict != c.want {
			t.Fatalf("EffectWindow %v: %s, want %s\n%s", c.window, rep.Verdict, c.want, rep.Text(5))
		}
	}
}

// Benchmarks of the search's cost on one hot resource: legal histories are cheap at any
// overlap; a history the search must reject costs about 2^overlap per op. Run with
// go test ./check -run XXX -bench HotKey -benchtime 1x.
func BenchmarkHotKey(b *testing.B) {
	for _, race := range []bool{false, true} {
		for _, clients := range []int{4, 8, 16} {
			b.Run(fmt.Sprintf("clients=%d/race=%v", clients, race), func(b *testing.B) {
				p := defaultSim(11)
				p.Resources, p.Clients, p.Epochs, p.OpsPerClient = 1, clients, 1, 2000/clients
				p.MeanThink = time.Millisecond
				p.Bugs.Race = race
				h := p.history()
				for b.Loop() {
					rep := check.Check(h, []check.AnySpec{bkSpec(p.Capacity)}, check.Options{MemoryBytes: 1 << 30, PartitionTimeout: 60 * time.Second})
					part := rep.Specs[0].Partitions[0]
					b.ReportMetric(float64(part.Steps), "steps")
					b.ReportMetric(float64(rep.PeakHeapBytes)/1e6, "peakMB")
					b.ReportMetric(float64(part.MaxInFlight), "inflight")
					if (rep.Verdict == check.Fail) != race && rep.Verdict != check.Unknown {
						b.Fatalf("race=%v got %s", race, rep.Verdict)
					}
				}
			})
		}
	}
}

// An oversized history (64 clients on one resource, racing, so the search must reject it) under a
// small budget ends UNKNOWN with coverage instead of exhausting memory, while the other
// partitions are still checked. Run with go test ./check -run XXX -bench Oversized -benchtime 1x.
func BenchmarkOversizedHistoryStaysInBudget(b *testing.B) {
	p := defaultSim(12)
	p.Resources, p.Clients, p.Epochs, p.OpsPerClient = 1, 64, 1, 4000/64
	p.MeanThink = time.Millisecond
	p.Bugs.Race = true
	hot := p.history()
	cold := defaultSim(13)
	cold.Epochs = 1
	calls, _ := hot.Phase(0)
	other, _ := cold.history().Phase(0)
	for _, c := range other { // resources 1 to 4 next to the hot resource 0
		var in bkIn
		var out bkOut
		json.Unmarshal(c.Info, &in)
		if !c.Lost() {
			json.Unmarshal(c.Steps[0].Body, &out)
		}
		if out.Name == initialName(in.Res) {
			out.Name = initialName(in.Res + 1)
		}
		in.Res++
		c.Info, _ = json.Marshal(in)
		if !c.Lost() {
			c.Steps[0].Body, _ = json.Marshal(out)
		}
		calls = append(calls, c)
	}
	h := check.NewHistory([][]check.Call{calls}, nil)
	for b.Loop() {
		rep := check.Check(h, []check.AnySpec{bkSpec(p.Capacity)}, check.Options{MemoryBytes: 256 << 20, PartitionTimeout: 20 * time.Second})
		s := rep.Specs[0]
		b.ReportMetric(float64(rep.PeakHeapBytes)/1e6, "peakMB")
		b.ReportMetric(float64(s.Coverage.OpsUnchecked), "unchecked")
		b.Logf("%s", rep.Reason)
		if rep.Verdict == check.Pass || s.Coverage.ByStatus[check.StatusOK] < 4 {
			b.Fatalf("got %s %v", rep.Verdict, s.Coverage.ByStatus)
		}
	}
}
