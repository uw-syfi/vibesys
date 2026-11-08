package flow_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// served is a plan of a task with a job-scoped server.
type served struct {
	kind    core.JobKind
	samples uint32
	shared  bool // the check runs against the server too
	linked  bool // a measure job links a saved pass instead of running the check
}

func (s served) String() string {
	return fmt.Sprintf("%s/samples=%d/shared=%v/linked=%v", s.kind, s.samples, s.shared, s.linked)
}

const (
	startupOf = 40 * time.Minute
	resetOf   = 2 * time.Minute
)

func (s served) plan() flow.Plan {
	p := shape{kind: s.kind, protocol2: true, samples: s.samples, linked: s.linked}.plan()
	p.Service = &task.Service{
		Serve: []string{"${PROJECT_ROOT}/_server.py", "--port", "${PORT}"}, Ready: "/health", StartupTimeout: startupOf,
		Reset:          task.Command{Argv: []string{"${PROJECT_ROOT}/reset.py"}, Timeout: resetOf},
		ShareWithCheck: s.shared,
	}
	return p
}

func servedShapes() []served {
	var out []served
	for _, shared := range []bool{false, true} {
		out = append(out, served{kind: core.JobCheck, shared: shared})
		for _, linked := range []bool{false, true} {
			for _, n := range []uint32{1, 2, 4} {
				out = append(out, served{kind: core.JobMeasure, samples: n, shared: shared, linked: linked})
			}
		}
	}
	return out
}

// serverFault is how a serving world answers one request instead of well.
type serverFault string

const (
	serverFine       serverFault = ""
	serverNotReady   serverFault = "the server is not ready"
	serverExitsEarly serverFault = "the server exits while it starts"
	serverCannotRun  serverFault = "the server cannot be started"
	serverUnseen     serverFault = "the server's start is not seen to end"
	resetFails       serverFault = "the reset exits non-zero"
	resetUnseen      serverFault = "the reset is not seen to end"
	serverDiesInRun  serverFault = "the server exits while a command runs"
	cancelHere       serverFault = "cancelled between requests"
)

// servingWorld answers like a machine whose job-scoped server ends at the probes dead names: dead
// says, for each ProbeService in turn, that the server ended. At the request numbered at, it
// answers with the fault instead.
type servingWorld struct {
	world
	dead    []bool
	fault   serverFault
	faultAt int

	probes int
}

func (w *servingWorld) Do(ctx context.Context, r flow.Request) flow.Event {
	n := len(w.asked)
	if n != w.faultAt || w.fault == serverFine {
		switch r := r.(type) {
		case flow.Serve:
			w.asked = append(w.asked, r)
			return w.answered(flow.Serving{})
		case flow.ProbeService:
			w.asked = append(w.asked, r)
			alive := w.probes >= len(w.dead) || !w.dead[w.probes]
			w.probes++
			return w.answered(flow.ServiceState{Alive: alive})
		}
		return w.world.Do(ctx, r)
	}
	w.asked = append(w.asked, r)
	switch w.fault {
	case serverNotReady:
		return w.answered(flow.Ran{Exec: core.ExecOutcome{Kind: core.ExecServiceNotReady}, ServiceLog: "loading weights 12/96"})
	case serverExitsEarly, serverDiesInRun:
		return w.answered(flow.Ran{Exec: core.ExecOutcome{Kind: core.ExecServiceExited}, ServiceLog: "HIP out of memory"})
	case serverCannotRun:
		return w.answered(flow.NotRan{Failure: core.Failure{Kind: core.FailureUnavailable, Message: "no allocation"}})
	case serverUnseen, resetUnseen:
		return w.answered(flow.Unseen{Failure: core.Failure{Kind: core.FailureUnavailable, Message: "the step is gone"}})
	case resetFails:
		return w.answered(flow.Ran{Exec: core.ExecOutcome{Kind: core.ExecExited, ExitCode: 4}})
	case cancelHere:
		return w.answered(flow.Cancelled{})
	}
	return w.answered(flow.Cancelled{})
}

func (w *servingWorld) answered(e flow.Event) flow.Event {
	w.answers = append(w.answers, e)
	return e
}

// applies says whether a fault fits the request: a server's start fails only a Serve, a reset's
// only a reset, and a server that ends under a command only a command on it.
func (f serverFault) applies(r flow.Request) bool {
	run, isRun := r.(flow.Run)
	switch f {
	case serverNotReady, serverExitsEarly, serverCannotRun, serverUnseen:
		_, ok := r.(flow.Serve)
		return ok
	case resetFails, resetUnseen:
		return isRun && run.Stage == flow.StageReset
	case serverDiesInRun:
		return isRun && run.OnService
	}
	return true
}

// checkServed checks every invariant of a job-scoped server in one run of the flow.
func checkServed(t *testing.T, label string, s served, w *servingWorld, v flow.Verdict) {
	t.Helper()
	p := s.plan()
	fail := func(format string, args ...any) {
		t.Helper()
		t.Fatalf("%s: %s\nasked: %+v\nanswers: %+v\nverdict: %+v", label, fmt.Sprintf(format, args...), w.asked, w.answers, v)
	}
	serves, checks := 0, false
	for i, r := range w.asked {
		var prev flow.Request
		var prevAnswer flow.Event
		if i > 0 {
			prev, prevAnswer = w.asked[i-1], w.answers[i-1]
		}
		switch r := r.(type) {
		case flow.Serve:
			serves++
			// The first start is a start, every later one a restart after a probe found it ended.
			if again := serves > 1; r.Again != again {
				fail("start %d says again=%v", serves, r.Again)
			}
			if r.Again {
				if _, probed := prev.(flow.ProbeService); !probed || prevAnswer != (flow.ServiceState{Alive: false}) {
					fail("a restart at %d follows %+v answered %+v, not a probe that found the server ended", i, prev, prevAnswer)
				}
			}
			if s.kind == core.JobCheck && !s.shared {
				fail("a check job whose check does not share the server started one")
			}
			if strings.Join(r.Argv, " ") != "/work/checkout/_server.py --port ${PORT}" || r.Ready != "/health" || r.StartupTimeout != startupOf {
				fail("the server starts as %+v", r)
			}
			// Its lifetime covers its startup and every command still to run against it.
			left := int(p.Samples) - samplesBefore(w.asked[:i])
			if want := startupOf + time.Duration(max(left, 0))*(p.Benchmark.Timeout+resetOf); r.Lifetime < want {
				fail("start %d lives %v, and %d samples are left: want at least %v", serves, r.Lifetime, left, want)
			}
		case flow.ProbeService:
			if serves == 0 {
				fail("a probe before any server")
			}
		case flow.Run:
			switch r.Stage {
			case flow.StageCheck:
				checks = true
				if r.OnService != s.shared {
					fail("the check runs on the server: %v, shared %v", r.OnService, s.shared)
				}
				if s.shared && !slices.ContainsFunc(w.asked[:i], func(q flow.Request) bool { _, ok := q.(flow.Serve); return ok }) {
					fail("a shared check ran before the server started")
				}
			case flow.StageReset:
				// A reset follows a probe that found the server running, and nothing else.
				if _, probed := prev.(flow.ProbeService); !probed || prevAnswer != (flow.ServiceState{Alive: true}) {
					fail("a reset at %d follows %+v answered %+v", i, prev, prevAnswer)
				}
				if !r.OnService || strings.Join(r.Argv, " ") != "/work/checkout/reset.py" || r.Timeout != resetOf {
					fail("the reset runs as %+v", r)
				}
			case flow.StageBenchmark:
				if !r.OnService {
					fail("a sample ran off the server")
				}
				// Every sample follows the start of its server or a reset that passed.
				_, started := prev.(flow.Serve)
				reset, wasRun := prev.(flow.Run)
				if !started && (!wasRun || reset.Stage != flow.StageReset) {
					fail("sample at %d follows %+v, neither a start nor a reset", i, prev)
				}
			default:
				fail("the %s step ran", r.Stage)
			}
		default:
			// The checkout and the observations are the flow's own, and other tests hold them.
		}
	}
	if s.kind == core.JobMeasure && !s.shared && checks && serves > 0 {
		// The server starts after the check that does not share it.
		first := slices.IndexFunc(w.asked, func(q flow.Request) bool { _, ok := q.(flow.Serve); return ok })
		check := slices.IndexFunc(w.asked, func(q flow.Request) bool { r, ok := q.(flow.Run); return ok && r.Stage == flow.StageCheck })
		if first < check {
			fail("the server started before a check that does not share it")
		}
	}
	passed, ok := v.(flow.Passed)
	if !ok {
		if w.fault == serverFine {
			fail("failed with no fault")
		}
		if len(w.asked) != w.faultAt+1 {
			fail("the fault was at %d, and %d requests were made", w.faultAt, len(w.asked))
		}
		stage, reason := flow.Ending(v)
		want := map[serverFault]flow.Stage{serverNotReady: flow.StageService, serverExitsEarly: flow.StageService, serverCannotRun: flow.StageService, serverUnseen: flow.StageService, resetFails: flow.StageReset, resetUnseen: flow.StageReset}[w.fault]
		if want != "" && stage != want {
			fail("ended in the %s step, want %s", stage, want)
		}
		if w.fault == resetFails && !strings.Contains(reason, fmt.Sprintf("before sample %d", samplesBefore(w.asked)+1)) {
			fail("a failed reset names the sample it fails: %q", reason)
		}
		if (w.fault == serverNotReady || w.fault == serverExitsEarly || w.fault == serverDiesInRun) && (!strings.Contains(reason, "job's server") || !strings.Contains(reason, "out of memory") && !strings.Contains(reason, "loading weights")) {
			fail("the reason %q does not name the server and its last lines", reason)
		}
		return
	}
	if w.fault != serverFine {
		fail("passed with %s at %d", w.fault, w.faultAt)
	}
	f := passed.Findings
	if s.kind == core.JobCheck {
		if f.Services != nil {
			fail("a check job records the servers of samples: %v", f.Services)
		}
		return
	}
	if want := 1 + countDead(w); serves != want {
		fail("%d starts, want one and one for each of the %d ended servers", serves, want-1)
	}
	// Each sample's server is what the request before it says: a first start, a restart, or a
	// reset of a server an earlier command used.
	var want []core.SampleService
	for i, r := range w.asked {
		if run, ok := r.(flow.Run); ok && run.Stage == flow.StageBenchmark {
			switch prev := w.asked[i-1].(type) {
			case flow.Serve:
				want = append(want, map[bool]core.SampleService{false: core.SampleServiceStarted, true: core.SampleServiceRestarted}[prev.Again])
			default:
				want = append(want, core.SampleServiceReused)
			}
		}
	}
	if !reflect.DeepEqual(f.Services, want) || len(f.Services) != len(f.Samples) {
		fail("the samples' servers are %v, want %v", f.Services, want)
	}
	if s.shared && !s.linked && f.Services[0] == core.SampleServiceStarted {
		fail("the first sample after a check on the server ran on a server nothing used: %v", f.Services)
	}
	res := p.Record("j-run", commit, f, epoch)
	if got := res.Outcome.(core.MeasureOutcome).Services; !reflect.DeepEqual(got, want) {
		fail("the result records %v", got)
	}
	if _, err := running(p).Apply(core.Transition{Job: "j-run", From: core.JobRunning, To: core.JobSucceeded, At: epoch, Result: &res}); err != nil {
		fail("the result is not accepted: %v", err)
	}
}

func samplesBefore(asked []flow.Request) int {
	n := 0
	for _, r := range asked {
		if run, ok := r.(flow.Run); ok && run.Stage == flow.StageBenchmark {
			n++
		}
	}
	return n
}

func countDead(w *servingWorld) int {
	n := 0
	for i := 0; i < w.probes && i < len(w.dead); i++ {
		if w.dead[i] {
			n++
		}
	}
	return n
}

// Over seeded random plans, servers that end between any samples and a fault of any kind at any
// request, the job starts its server once and again only for one that ended, resets it before every
// sample that reuses it, runs every sample against it, records which server each sample ran on, and
// a failed start or reset ends the job there with nothing saved.
func TestAJobScopedServerIsStartedOnceResetBeforeEachReuseAndStartedAgainWhenItEnded(t *testing.T) {
	shapes := servedShapes()
	faults := []serverFault{serverFine, serverFine, serverFine, serverNotReady, serverExitsEarly, serverCannotRun, serverUnseen, resetFails, resetUnseen, serverDiesInRun, cancelHere}
	for seed := uint64(0); seed < 3000; seed++ {
		r := rand.New(rand.NewPCG(seed, 17))
		s := shapes[r.IntN(len(shapes))]
		p := s.plan()
		dead := make([]bool, 8)
		for i := range dead {
			dead[i] = r.IntN(3) == 0
		}
		w := &servingWorld{world: world{p: p, at: -1}, dead: dead, fault: faults[r.IntN(len(faults))]}
		// A dry run says how many requests there are, and the fault goes at one it fits.
		dry := &servingWorld{world: world{p: p, at: -1}, dead: dead}
		if _, err := flow.Drive(context.Background(), p, dry); err != nil {
			t.Fatal(err)
		}
		if w.fault != serverFine {
			var fits []int
			for i, q := range dry.asked {
				if w.fault.applies(q) {
					fits = append(fits, i)
				}
			}
			if len(fits) == 0 {
				w.fault = serverFine
			} else {
				w.faultAt = fits[r.IntN(len(fits))]
			}
		}
		v, err := flow.Drive(context.Background(), p, w)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		checkServed(t, fmt.Sprintf("seed %d, %s, %s at %d, dead %v", seed, s, w.fault, w.faultAt, dead), s, w, v)
	}
}

// The exact sequence of a measure job of three samples whose server ends after the second: the
// server starts once after the check, the second sample follows a reset, and the third a restart.
func TestAMeasureJobOfThreeSamplesWhoseServerEndsAfterTheSecond(t *testing.T) {
	s := served{kind: core.JobMeasure, samples: 3}
	p := s.plan()
	w := &servingWorld{world: world{p: p, at: -1}, dead: []bool{false, true}}
	v, err := flow.Drive(context.Background(), p, w)
	if err != nil {
		t.Fatal(err)
	}
	var steps []string
	for _, r := range w.asked {
		switch r := r.(type) {
		case flow.Run:
			steps = append(steps, string(r.Stage))
		case flow.Serve:
			steps = append(steps, map[bool]string{false: "serve", true: "serve-again"}[r.Again])
		case flow.ProbeService:
			steps = append(steps, "probe")
		case flow.Observe:
			steps = append(steps, "observe")
		case flow.Checkout:
			steps = append(steps, "checkout")
		}
	}
	want := "checkout check observe serve benchmark probe reset benchmark probe serve-again benchmark observe"
	if got := strings.Join(steps, " "); got != want {
		t.Fatalf("steps\n%s\nwant\n%s", got, want)
	}
	if got := passedOf(t, v).Services; !reflect.DeepEqual(got, []core.SampleService{core.SampleServiceStarted, core.SampleServiceReused, core.SampleServiceRestarted}) {
		t.Fatalf("services %v", got)
	}
	checkServed(t, "three samples", s, w, v)
}

// A plan whose job-scoped server cannot be followed never starts.
func TestAPlanWithAServerThatCannotBeFollowedNeverStarts(t *testing.T) {
	for name, mutate := range map[string]func(*flow.Plan){
		"a server with no command":    func(p *flow.Plan) { p.Service.Serve = nil },
		"a readiness that is no path": func(p *flow.Plan) { p.Service.Ready = "health" },
		"no startup time":             func(p *flow.Plan) { p.Service.StartupTimeout = 0 },
		"a reset with no command":     func(p *flow.Plan) { p.Service.Reset.Argv = nil },
		"a reset with no time limit":  func(p *flow.Plan) { p.Service.Reset.Timeout = 0 },
		"a candidate besides": func(p *flow.Plan) {
			p.Candidate = shape{kind: core.JobCheck, candidate: true}.plan().Candidate
		},
	} {
		p := served{kind: core.JobMeasure, samples: 2}.plan()
		mutate(&p)
		if _, err := flow.Drive(context.Background(), p, &servingWorld{world: world{p: p, at: -1}}); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

// randomServingEvent is any event, the server's answers among them.
func randomServingEvent(r *rand.Rand) flow.Event {
	switch r.IntN(14) {
	case 10:
		return flow.Serving{}
	case 11:
		return flow.ServiceState{Alive: true}
	case 12:
		return flow.ServiceState{Alive: false}
	case 13:
		return flow.Ran{Exec: core.ExecOutcome{Kind: core.ExecServiceNotReady}}
	}
	return randomEvent(r)
}

// Whatever events a plan with a job-scoped server is given, the machine stops within six steps and
// three for each sample, with a coherent verdict that is final, and a pass that carries one server
// per sample.
func TestAnyEventSequenceOfAServedPlanTerminatesWithACoherentVerdict(t *testing.T) {
	shapes := servedShapes()
	for seed := uint64(0); seed < 3000; seed++ {
		r := rand.New(rand.NewPCG(seed, 23))
		p := shapes[int(seed)%len(shapes)].plan()
		state, req, err := flow.Start(p)
		if err != nil {
			t.Fatal(err)
		}
		bound := 6 + 3*int(p.Samples)
		for steps := 0; req != nil; steps++ {
			if steps > bound {
				t.Fatalf("seed %d: more than %d steps", seed, bound)
			}
			state, req = flow.Step(state, randomServingEvent(r))
		}
		v := state.Verdict()
		if passed, ok := v.(flow.Passed); ok {
			f := passed.Findings
			if p.Kind == core.JobMeasure && (len(f.Samples) != int(p.Samples) || len(f.Services) != len(f.Samples)) {
				t.Fatalf("seed %d: passed with %d samples and %d servers of %d", seed, len(f.Samples), len(f.Services), p.Samples)
			}
		} else if _, reason := flow.Ending(v); v == nil || reason == "" {
			t.Fatalf("seed %d: %+v", seed, v)
		}
		again, next := flow.Step(state, flow.Serving{})
		if next != nil || !reflect.DeepEqual(again.Verdict(), v) {
			t.Fatalf("seed %d: a step after the verdict changed it", seed)
		}
	}
}
