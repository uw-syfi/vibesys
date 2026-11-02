package flow_test

import (
	"context"
	"fmt"
	"math"
	"math/rand/v2"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

var (
	epoch  = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	commit = core.Commit(strings.Repeat("ab", 20))
	print_ = core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"os": "linux"}}
)

// shape is one kind of plan the flow can be asked to follow.
type shape struct {
	kind      core.JobKind
	build     bool
	linked    bool // a measure job links a saved pass instead of running the check
	protocol2 bool
	samples   uint32
}

func (s shape) String() string {
	return fmt.Sprintf("%s/build=%v/linked=%v/protocol2=%v/samples=%d", s.kind, s.build, s.linked, s.protocol2, s.samples)
}

func (s shape) plan() flow.Plan {
	p := flow.Plan{
		Kind: s.kind, Task: "t", TaskVersion: "v1", Key: "k1", Environment: print_,
		Check:  task.Command{Argv: []string{"${PROJECT_ROOT}/check.sh"}, Timeout: time.Minute},
		Metric: core.Metric{Name: "ops", Direction: core.Maximize},
	}
	if s.build {
		p.Build = &task.Command{Argv: []string{"make"}, Timeout: time.Minute}
	}
	if s.kind == core.JobMeasure {
		p.Samples = s.samples
		p.Benchmark = task.Command{Argv: []string{"${PROJECT_ROOT}/bench.sh", "--fast"}, Timeout: time.Minute}
		p.Result = task.Result{JSONArgument: "--out", Field: "ops"}
		if s.protocol2 {
			p.Result = task.Result{Protocol2: true}
		}
		if s.linked {
			p.CheckPass = "j-pass"
		}
	}
	return p
}

func shapes() []shape {
	out := []shape{
		{kind: core.JobCheck}, {kind: core.JobCheck, build: true},
	}
	for _, build := range []bool{false, true} {
		for _, linked := range []bool{false, true} {
			for _, p2 := range []bool{false, true} {
				for _, n := range []uint32{1, 3} {
					out = append(out, shape{kind: core.JobMeasure, build: build, linked: linked, protocol2: p2, samples: n})
				}
			}
		}
	}
	return out
}

// fault is how a world answers one request instead of well.
type fault string

const (
	none          fault = ""
	exitsNonZero  fault = "exits non-zero"
	timesOut      fault = "times out"
	cancelledRun  fault = "cancelled while running"
	cancelEvent   fault = "cancelled between requests"
	unknownEnding fault = "ends unknown"
	cannotRun     fault = "cannot run"
	noFile        fault = "writes no file"
	badFile       fault = "writes a bad file"
	unreadable    fault = "writes an unreadable file"
	noCheckout    fault = "no checkout"
	wrongAnswer   fault = "answers with the wrong event"
)

// world answers requests like a machine, with at most one fault at the request numbered at.
type world struct {
	p     flow.Plan
	at    int
	fault fault

	asked   []flow.Request
	answers []flow.Event
	values  []float64 // what each sample run really measured
}

func (w *world) Do(_ context.Context, r flow.Request) flow.Event {
	n := len(w.asked)
	w.asked = append(w.asked, r)
	e := w.answer(n, r)
	w.answers = append(w.answers, e)
	return e
}

func (w *world) faulty(n int) fault {
	if n == w.at {
		return w.fault
	}
	return none
}

func (w *world) answer(n int, r flow.Request) flow.Event {
	f := w.faulty(n)
	if f == cancelEvent {
		return flow.Cancelled{}
	}
	if f == wrongAnswer {
		if _, observing := r.(flow.Observe); observing {
			return flow.CheckedOut{Dir: "/d", Scratch: "/s"}
		}
		return flow.Observed{}
	}
	switch r := r.(type) {
	case flow.Checkout:
		if f == noCheckout {
			return flow.CheckoutFailed{Failure: core.Failure{Kind: core.FailureNotFound, Message: "no such commit"}}
		}
		return flow.CheckedOut{Dir: "/work/checkout", Scratch: "/work/scratch"}
	case flow.Observe:
		return flow.Observed{
			Machine: core.MachineFacts{HostID: "host-a", OS: "linux", Arch: "amd64", LogicalCPUs: 8},
			Load:    core.MachineLoad{SampledAt: epoch.Add(time.Duration(n) * time.Second), CPUBusyFraction: 0.5},
		}
	case flow.Run:
		started := epoch.Add(time.Duration(n) * time.Minute)
		ran := flow.Ran{Exec: core.ExecOutcome{Kind: core.ExecExited}, Started: started, Ended: started.Add(3 * time.Second)}
		switch f {
		case exitsNonZero:
			ran.Exec.ExitCode = 3
		case timesOut:
			ran.Exec = core.ExecOutcome{Kind: core.ExecTimedOut}
		case cancelledRun:
			ran.Exec = core.ExecOutcome{Kind: core.ExecCancelled}
		case unknownEnding:
			ran.Exec = core.ExecOutcome{Kind: core.ExecUnknown}
		case cannotRun:
			return flow.NotRan{Failure: core.Failure{Kind: core.FailureUnavailable, Message: "no environment"}}
		}
		if r.Read != "" {
			ran.Found = f != noFile
			ran.Output = w.output(len(w.values), f)
			ran.ReadError = map[bool]string{true: "too large", false: ""}[f == unreadable]
			if ran.Found && f != badFile && f != unreadable && ran.Exec.Kind == core.ExecExited && ran.Exec.ExitCode == 0 {
				w.values = append(w.values, w.value(len(w.values)))
			}
		}
		return ran
	}
	return flow.Cancelled{}
}

func (w *world) value(i int) float64 { return float64(100*(i+1)) + 0.5 }

func (w *world) output(i int, f fault) []byte {
	if f == badFile {
		return []byte(`{"ops": "fast"}` + "\n[")
	}
	if w.p.Result.Protocol2 {
		return []byte(fmt.Sprintf(`{"kind":"hello","protocol":2,"metrics":{"ops":{},"p99":{"required":false}}}`+"\n"+`{"kind":"result","values":{"ops":%v,"p99":%v}}`, w.value(i), float64(i)+0.25))
	}
	return []byte(fmt.Sprintf(`{"ops": %v, "noise": "x"}`, w.value(i)))
}

// stageOf says which stage a request belongs to.
func stageOf(r flow.Request) flow.Stage {
	switch r := r.(type) {
	case flow.Checkout:
		return flow.StageCheckout
	case flow.Observe:
		return flow.StageObserve
	case flow.Run:
		return r.Stage
	}
	return ""
}

// expected is the stages a plan takes when nothing goes wrong, in order.
func expected(p flow.Plan) []flow.Stage {
	out := []flow.Stage{flow.StageCheckout}
	if p.Build != nil {
		out = append(out, flow.StageBuild)
	}
	if p.RunsCheck() {
		out = append(out, flow.StageCheck)
	}
	if p.Kind == core.JobMeasure {
		out = append(out, flow.StageObserve)
		for i := uint32(0); i < p.Samples; i++ {
			out = append(out, flow.StageBenchmark)
		}
	}
	return append(out, flow.StageObserve)
}

func running(p flow.Plan) core.Job {
	var settings core.Settings = core.CheckSettings{}
	if p.Kind == core.JobMeasure {
		settings = core.MeasureSettings{Samples: p.Samples}
	}
	env := p.Environment
	return core.Job{
		ID: "j-run", Key: p.Key, State: core.JobRunning, Environment: &env,
		Spec: core.JobSpec{Commit: commit, Environment: core.EnvironmentHost, Settings: settings},
	}
}

// checkRun checks every invariant of one run of the flow against a world that answered it.
func checkRun(t *testing.T, label string, p flow.Plan, w *world, v flow.Verdict) {
	t.Helper()
	fail := func(format string, args ...any) {
		t.Helper()
		t.Fatalf("%s: %s\nasked: %v\nverdict: %+v", label, fmt.Sprintf(format, args...), w.asked, v)
	}
	want := expected(p)
	stages := make([]flow.Stage, len(w.asked))
	for i, r := range w.asked {
		stages[i] = stageOf(r)
	}
	// Whatever happens, the requests are a prefix of the plan's steps, in order.
	if len(stages) > len(want) {
		fail("asked for %d steps, the plan has %d", len(stages), len(want))
	}
	for i := range stages {
		if stages[i] != want[i] {
			fail("step %d is %s, want %s", i, stages[i], want[i])
		}
	}
	if (v.Findings != nil) == (v.NotAttempted != nil || v.Rejected != "" || v.Exec.Kind != core.ExecExited || v.Exec.ExitCode != 0) {
		fail("a verdict is either findings or one way of not passing")
	}
	if v.Passed() {
		if w.fault != none && w.at < len(want) && w.fault != none {
			fail("passed although the world was faulty (%s at %d)", w.fault, w.at)
		}
		if len(stages) != len(want) {
			fail("passed after %d of %d steps", len(stages), len(want))
		}
		if v.Stage != "" || v.Reason != "" {
			fail("a pass names no ending")
		}
		f := v.Findings
		if p.Kind == core.JobMeasure {
			if len(f.Samples) != int(p.Samples) || len(w.values) != int(p.Samples) {
				fail("%d samples, %d measured, plan asks %d", len(f.Samples), len(w.values), p.Samples)
			}
			for i, s := range f.Samples {
				if s != w.values[i] || math.IsNaN(s) || math.IsInf(s, 0) {
					fail("sample %d = %v, the benchmark measured %v", i, s, w.values[i])
				}
			}
			for _, o := range f.Observations {
				if len(o.Values) != len(f.Samples) {
					fail("observation %s has %d values for %d samples", o.Name, len(o.Values), len(f.Samples))
				}
			}
		} else if len(f.Samples) != 0 || len(f.Observations) != 0 {
			fail("a check job has samples")
		}
		if f.Machine.HostID != "host-a" || f.Load.SampledAt.IsZero() {
			fail("a pass records the machine: %+v %+v", f.Machine, f.Load)
		}
		// What it would save is accepted by the same rule every store applies.
		at := epoch.Add(time.Hour)
		res := p.Record("j-run", commit, *f, at)
		if _, err := running(p).Apply(core.Transition{Job: "j-run", From: core.JobRunning, To: core.JobSucceeded, At: at, Result: &res}); err != nil {
			fail("the result is not accepted: %v", err)
		}
		if m, ok := res.Outcome.(core.MeasureOutcome); ok && (m.CheckJob == "" || len(m.Samples) == 0) {
			fail("a measurement lacks its check or its samples: %+v", m)
		}
		return
	}
	// Anything else saves nothing, and ends the flow where it ended: the last request is the
	// one that was answered badly, and none follows it.
	if v.Findings != nil {
		fail("a job that did not pass has findings")
	}
	if w.fault == none {
		fail("failed in a world with no fault")
	}
	if len(stages) != w.at+1 {
		fail("fault at request %d, but %d requests were made", w.at, len(stages))
	}
	if v.Stage != stages[w.at] {
		fail("ended in %s, the faulty request was %s", v.Stage, stages[w.at])
	}
	if v.Reason == "" {
		fail("an ending says why")
	}
	switch w.fault {
	case cancelEvent, cancelledRun:
		if !v.Cancelled() {
			fail("a cancel is a cancelled verdict")
		}
	case exitsNonZero:
		if v.Exec.Kind != core.ExecExited || v.Exec.ExitCode != 3 {
			fail("exit code not carried")
		}
	case timesOut:
		if v.Exec.Kind != core.ExecTimedOut {
			fail("time out not carried")
		}
	case unknownEnding:
		if v.Exec.Kind != core.ExecUnknown {
			fail("unknown ending not carried")
		}
	case cannotRun, noCheckout, wrongAnswer:
		if v.NotAttempted == nil {
			fail("an unattempted step is reported as such")
		}
	case noFile, badFile, unreadable:
		if v.Rejected == "" {
			fail("an unreadable result is a rejection")
		}
	}
}

// Every plan the flow can be given, with a fault of every kind at every one of its steps, ends
// where the fault is, saves nothing, and asks for nothing after. It is exhaustive for the steps
// and the faults, so no combination is left to chance.
func TestAFaultAtEveryStepEndsTheJobThereAndSavesNothing(t *testing.T) {
	faults := []fault{exitsNonZero, timesOut, cancelledRun, cancelEvent, unknownEnding, cannotRun, noFile, badFile, unreadable, noCheckout, wrongAnswer}
	for _, s := range shapes() {
		p := s.plan()
		steps := len(expected(p))
		for at := 0; at < steps; at++ {
			for _, f := range faults {
				w := &world{p: p, at: at, fault: f}
				v, err := flow.Drive(context.Background(), p, w)
				if err != nil {
					t.Fatal(err)
				}
				stage := expected(p)[at]
				if applies(f, stage) {
					checkRun(t, fmt.Sprintf("%s, %s at %d (%s)", s, f, at, stage), p, w, v)
				} else {
					// The fault has no meaning at this step (an exit code of an observation),
					// so the world answers well and the job passes.
					w.fault = none
					w2 := &world{p: p, at: -1}
					v2, _ := flow.Drive(context.Background(), p, w2)
					checkRun(t, fmt.Sprintf("%s, no fault", s), p, w2, v2)
				}
			}
		}
	}
}

// applies says whether a fault can happen at a stage: only a command exits, only a benchmark
// writes a file, only a checkout is refused. A cancel and a wrong answer can come at any step.
func applies(f fault, s flow.Stage) bool {
	switch f {
	case exitsNonZero, timesOut, cancelledRun, unknownEnding, cannotRun:
		return s == flow.StageBuild || s == flow.StageCheck || s == flow.StageBenchmark
	case noFile, badFile, unreadable:
		return s == flow.StageBenchmark
	case noCheckout:
		return s == flow.StageCheckout
	}
	return true
}

func TestAWorldWithNoFaultPassesWithExactlyWhatWasMeasured(t *testing.T) {
	for _, s := range shapes() {
		p := s.plan()
		w := &world{p: p, at: -1}
		v, err := flow.Drive(context.Background(), p, w)
		if err != nil {
			t.Fatal(err)
		}
		if !v.Passed() {
			t.Fatalf("%s: %+v", s, v)
		}
		checkRun(t, s.String(), p, w, v)
		if got := len(w.asked); got != len(expected(p)) {
			t.Fatalf("%s: %d requests, want %d", s, got, len(expected(p)))
		}
	}
}

func TestACheckJobTimesTheCheckAndAMeasureJobGathersWhatTheBenchmarkReports(t *testing.T) {
	check := &world{p: shape{kind: core.JobCheck}.plan(), at: -1}
	v, _ := flow.Drive(context.Background(), check.p, check)
	if v.Findings.CheckTook != 3*time.Second {
		t.Errorf("the check took %v, the shell saw it take 3s", v.Findings.CheckTook)
	}
	m := shape{kind: core.JobMeasure, protocol2: true, samples: 3}
	w := &world{p: m.plan(), at: -1}
	v, _ = flow.Drive(context.Background(), w.p, w)
	obs := v.Findings.Observations
	if len(obs) != 1 || obs[0].Name != "p99" || fmt.Sprint(obs[0].Values) != "[0.25 1.25 2.25]" {
		t.Errorf("observations %+v", obs)
	}
	run := w.asked[len(w.asked)-2].(flow.Run)
	if got := strings.Join(run.Argv, " "); got != "/work/checkout/bench.sh --fast --vs-output /work/scratch/sample-2.out" {
		t.Errorf("the third sample runs %q", got)
	}
}

func TestACheckJobsCheckRunsInTheCheckoutAndALinkedPassRunsNone(t *testing.T) {
	linked := shape{kind: core.JobMeasure, linked: true, samples: 1}
	w := &world{p: linked.plan(), at: -1}
	v, _ := flow.Drive(context.Background(), w.p, w)
	for _, r := range w.asked {
		if run, ok := r.(flow.Run); ok && run.Stage == flow.StageCheck {
			t.Fatal("a measure job with a linked pass ran the check")
		}
	}
	res := w.p.Record("j-run", commit, *v.Findings, epoch)
	if res.Outcome.(core.MeasureOutcome).CheckJob != "j-pass" {
		t.Errorf("check job %q, want the linked pass", res.Outcome.(core.MeasureOutcome).CheckJob)
	}
	own := shape{kind: core.JobMeasure, samples: 1}
	w = &world{p: own.plan(), at: -1}
	v, _ = flow.Drive(context.Background(), w.p, w)
	res = w.p.Record("j-run", commit, *v.Findings, epoch)
	if res.Outcome.(core.MeasureOutcome).CheckJob != "j-run" {
		t.Errorf("a measure job that ran its check names itself, got %q", res.Outcome.(core.MeasureOutcome).CheckJob)
	}
	first := w.asked[1].(flow.Run)
	if first.Stage != flow.StageCheck || first.Argv[0] != "/work/checkout/check.sh" {
		t.Errorf("the check runs %+v", first)
	}
}

func TestACancelledContextEndsTheJobBeforeTheNextRequest(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	p := shape{kind: core.JobMeasure, samples: 2}.plan()
	w := &world{p: p, at: -1}
	v, err := flow.Drive(ctx, p, w)
	if err != nil || !v.Cancelled() || len(w.asked) != 0 {
		t.Fatalf("verdict %+v, error %v, asked %v", v, err, w.asked)
	}
}

func TestAnInvalidPlanNeverStarts(t *testing.T) {
	good := shape{kind: core.JobMeasure, build: true, samples: 2}
	for name, mutate := range map[string]func(*flow.Plan){
		"a command that is not a program": func(p *flow.Plan) { p.Check.Argv = nil },
		"no time limit":                   func(p *flow.Plan) { p.Benchmark.Timeout = 0 },
		"a build with no limit":           func(p *flow.Plan) { p.Build.Timeout = 0 },
		"no samples":                      func(p *flow.Plan) { p.Samples = 0 },
		"no way to read the result":       func(p *flow.Plan) { p.Result = task.Result{} },
		"no field to read":                func(p *flow.Plan) { p.Result = task.Result{JSONArgument: "--out"} },
		"a metric without a direction":    func(p *flow.Plan) { p.Metric.Direction = "" },
		"no task":                         func(p *flow.Plan) { p.Task = "" },
		"no key":                          func(p *flow.Plan) { p.Key = "" },
		"another kind":                    func(p *flow.Plan) { p.Kind = core.JobCommand },
	} {
		p := good.plan()
		mutate(&p)
		if _, err := flow.Drive(context.Background(), p, &world{p: p, at: -1}); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	check := shape{kind: core.JobCheck}.plan()
	check.Samples = 1
	if check.Validate() == nil {
		t.Error("a check job with samples was accepted")
	}
}

// Seeded random runs: any mix of faults, at random requests and in random plans. Each seed is a
// run that replays exactly, and every invariant holds in all of them.
func TestSeededRandomRunsKeepEveryInvariant(t *testing.T) {
	all := shapes()
	faults := []fault{none, none, exitsNonZero, timesOut, cancelledRun, cancelEvent, unknownEnding, cannotRun, noFile, badFile, unreadable, noCheckout, wrongAnswer}
	for seed := uint64(0); seed < 2000; seed++ {
		r := rand.New(rand.NewPCG(seed, 99))
		p := all[r.IntN(len(all))].plan()
		stages := expected(p)
		at := r.IntN(len(stages))
		f := faults[r.IntN(len(faults))]
		if !applies(f, stages[at]) {
			f = none
		}
		w := &world{p: p, at: at, fault: f}
		v, err := flow.Drive(context.Background(), p, w)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		checkRun(t, fmt.Sprintf("seed %d (%s at %d)", seed, f, at), p, w, v)
	}
}

// The machine does not trust its shell: whatever events it is given, in whatever order, it stops
// after a bounded number of steps with a coherent verdict, and a verdict is final.
func randomEvent(r *rand.Rand) flow.Event {
	switch r.IntN(8) {
	case 0:
		return flow.CheckedOut{Dir: "/d", Scratch: "/s"}
	case 1:
		return flow.CheckedOut{}
	case 2:
		return flow.CheckoutFailed{Failure: core.Failure{Message: "x"}}
	case 3:
		return flow.Observed{}
	case 4:
		return flow.NotRan{Failure: core.Failure{Message: "x"}}
	case 5:
		return flow.Cancelled{}
	}
	kinds := []core.ExecKind{core.ExecExited, core.ExecExited, core.ExecExited, core.ExecTimedOut, core.ExecCancelled, core.ExecServiceExited, core.ExecUnknown, "weird"}
	out := []string{``, `{"ops": 1}`, `{"kind":"hello","protocol":2,"metrics":{"ops":{}}}` + "\n" + `{"kind":"result","values":{"ops":2}}`, `}{`}
	return flow.Ran{
		Exec:   core.ExecOutcome{Kind: kinds[r.IntN(len(kinds))], ExitCode: r.IntN(2)},
		Output: []byte(out[r.IntN(len(out))]), Found: r.IntN(4) != 0,
		Started: epoch, Ended: epoch.Add(time.Duration(r.IntN(5)-1) * time.Second),
	}
}

func runRandomEvents(t *testing.T, seed uint64, shapeIndex int) {
	t.Helper()
	all := shapes()
	r := rand.New(rand.NewPCG(seed, 5))
	p := all[shapeIndex%len(all)].plan()
	state, req, err := flow.Start(p)
	if err != nil {
		t.Fatal(err)
	}
	bound := 4 + 2*int(p.Samples) + 2
	steps := 0
	for req != nil {
		if steps++; steps > bound {
			t.Fatalf("seed %d: more than %d steps", seed, bound)
		}
		state, req = flow.Step(state, randomEvent(r))
	}
	if !state.Done() {
		t.Fatalf("seed %d: no request and not done", seed)
	}
	v := state.Verdict()
	if v.Passed() {
		if p.Kind == core.JobMeasure && len(v.Findings.Samples) != int(p.Samples) {
			t.Fatalf("seed %d: passed with %d samples of %d", seed, len(v.Findings.Samples), p.Samples)
		}
		for _, s := range v.Findings.Samples {
			if math.IsNaN(s) || math.IsInf(s, 0) {
				t.Fatalf("seed %d: non-finite sample", seed)
			}
		}
		if v.Findings.CheckTook < 0 {
			t.Fatalf("seed %d: negative check duration", seed)
		}
	} else if v.Findings != nil || v.Reason == "" {
		t.Fatalf("seed %d: %+v", seed, v)
	}
	// A verdict is final.
	again, next := flow.Step(state, flow.Observed{})
	if next != nil || again.Verdict().Passed() != v.Passed() || again.Verdict().Reason != v.Reason {
		t.Fatalf("seed %d: a step after the verdict changed it", seed)
	}
}

func TestAnyEventSequenceTerminatesWithACoherentVerdict(t *testing.T) {
	for seed := uint64(0); seed < 3000; seed++ {
		runRandomEvents(t, seed, int(seed))
	}
}

func FuzzAnyEventSequence(f *testing.F) {
	f.Add(uint64(1), uint8(0))
	f.Add(uint64(7), uint8(9))
	f.Fuzz(func(t *testing.T, seed uint64, shapeIndex uint8) {
		runRandomEvents(t, seed, int(shapeIndex))
	})
}

// Step leaves the state it was given as it was, so a run can be replayed or forked from any step.
func TestStepLeavesTheStateItWasGiven(t *testing.T) {
	p := shape{kind: core.JobMeasure, protocol2: true, samples: 3}.plan()
	w := &world{p: p, at: -1}
	state, req, _ := flow.Start(p)
	var snapshots []flow.State
	var requests []flow.Request
	for req != nil {
		snapshots, requests = append(snapshots, state), append(requests, req)
		state, req = flow.Step(state, w.Do(context.Background(), req))
	}
	// Replaying any step from its snapshot gives the same next request as the first time.
	for i, s := range snapshots {
		replay := &world{p: p, at: -1}
		replay.asked = w.asked[:i]
		replay.values = nil
		for j := 0; j < i; j++ {
			if run, ok := w.asked[j].(flow.Run); ok && run.Read != "" {
				replay.values = append(replay.values, w.value(len(replay.values)))
			}
		}
		_, next := flow.Step(s, replay.answer(i, requests[i]))
		if fmt.Sprint(next) != fmt.Sprint(requestAfter(requests, i)) {
			t.Fatalf("step %d replayed to %v, was %v", i, next, requestAfter(requests, i))
		}
	}
}

func requestAfter(requests []flow.Request, i int) flow.Request {
	if i+1 < len(requests) {
		return requests[i+1]
	}
	return nil
}
