package flowrun_test

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// backstop bounds every wait. It only keeps a hung test from hanging the build.
const backstop = time.Minute

var (
	commit = core.Commit(strings.Repeat("cd", 20))
	facts  = core.MachineFacts{HostID: "host-a", OS: "linux", Arch: "amd64", CPUModel: "Test CPU", LogicalCPUs: 8, MemoryBytes: 16 << 30}
	print_ = core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"os": "linux"}}
)

// prog is one command written for the fake environment's scripts and for a POSIX shell. The two
// renderings mean the same, and the scenarios below run on both: the fake is trusted where it
// answers as the real parts do.
type prog struct{ fake, real []string }

const marker = "UP"

func exits(code int) prog {
	return prog{fake: []string{"exit", strconv.Itoa(code)}, real: []string{"sh", "-c", fmt.Sprintf("exit %d", code)}}
}

// writes is a benchmark that writes the text to the file it is given and then exits with the code.
func writes(text string, code int) prog {
	return prog{
		fake: []string{"emit", text, ";", "exit", strconv.Itoa(code)},
		// The flow appends the flag and the path: they are $2 and $3 of the shell.
		real: []string{"sh", "-c", fmt.Sprintf(`printf '%%s' "$1" > "$3"; exit %d`, code), "sh", text},
	}
}

// hangs says marker on its own line, so a test knows it runs, and then waits to be ended.
func hangs() prog {
	return prog{
		fake: []string{"print", marker, ";", "hold", "never"},
		real: []string{"sh", "-c", "echo " + marker + "; exec sleep 600"},
	}
}

// missing is a program that does not exist.
func missing() prog {
	return prog{fake: []string{"no-such-command"}, real: []string{"/nonexistent/vibesys-no-such-program"}}
}

// scenario is a job and what must come of it.
type scenario struct {
	name    string
	kind    core.JobKind
	build   *prog
	check   prog
	bench   prog
	samples uint32
	linked  bool // a measure job links a saved check pass

	// want
	passed   bool
	stage    flow.Stage
	exec     core.ExecKind // when set, how the last command ended
	exitCode int
	rejected string // when set, a part of why the result was rejected
}

func ptr(p prog) *prog { return &p }

const goodJSON = `{"ops": 7.5, "ignored": "text"}`

func scenarios() []scenario {
	ok := exits(0)
	return []scenario{
		{name: "a check job passes", kind: core.JobCheck, check: ok, passed: true},
		{name: "a check job that exits non-zero fails in the check", kind: core.JobCheck, check: exits(1),
			stage: flow.StageCheck, exec: core.ExecExited, exitCode: 1},
		{name: "a measure job takes its samples", kind: core.JobMeasure, check: ok, bench: writes(goodJSON, 0), samples: 3, passed: true},
		{name: "a measure job that links a pass skips the check", kind: core.JobMeasure, check: exits(9), bench: writes(goodJSON, 0),
			samples: 2, linked: true, passed: true},
		{name: "a build runs first", kind: core.JobMeasure, build: ptr(ok), check: ok, bench: writes(goodJSON, 0), samples: 1, passed: true},
		{name: "a failing build ends the job in the build", kind: core.JobMeasure, build: ptr(exits(2)), check: ok, bench: writes(goodJSON, 0),
			samples: 1, stage: flow.StageBuild, exec: core.ExecExited, exitCode: 2},
		{name: "a failing check ends a measure job in the check", kind: core.JobMeasure, check: exits(4), bench: writes(goodJSON, 0),
			samples: 1, stage: flow.StageCheck, exec: core.ExecExited, exitCode: 4},
		{name: "a failing benchmark ends the job in the benchmark", kind: core.JobMeasure, check: ok, bench: exits(3),
			samples: 2, stage: flow.StageBenchmark, exec: core.ExecExited, exitCode: 3},
		{name: "a benchmark that exits non-zero fails even when it wrote a result", kind: core.JobMeasure, check: ok,
			bench: writes(goodJSON, 1), samples: 1, stage: flow.StageBenchmark, exec: core.ExecExited, exitCode: 1},
		{name: "a benchmark that wrote nothing is rejected", kind: core.JobMeasure, check: ok, bench: exits(0),
			samples: 1, stage: flow.StageBenchmark, rejected: "wrote no result"},
		{name: "a result that is not JSON is rejected", kind: core.JobMeasure, check: ok, bench: writes("not json", 0),
			samples: 1, stage: flow.StageBenchmark, rejected: "sample 1"},
		{name: "a result without the field is rejected", kind: core.JobMeasure, check: ok, bench: writes(`{"other": 1}`, 0),
			samples: 1, stage: flow.StageBenchmark, rejected: "sample 1"},
		{name: "a program that does not exist is never a pass", kind: core.JobMeasure, check: ok, bench: missing(),
			samples: 1, stage: flow.StageBenchmark},
	}
}

// plan makes the flow's plan for the scenario, with the rendering the fake or the shell reads.
func (s scenario) plan(real bool) flow.Plan {
	argv := func(p prog) []string {
		if real {
			return append([]string(nil), p.real...)
		}
		return append([]string(nil), p.fake...)
	}
	command := func(p prog) task.Command { return task.Command{Argv: argv(p), Timeout: time.Minute} }
	p := flow.Plan{
		Kind: s.kind, Task: "t", TaskVersion: "v1", Key: "k1", Environment: print_,
		Check:  command(s.check),
		Metric: core.Metric{Name: "ops", Direction: core.Maximize},
	}
	if s.build != nil {
		b := command(*s.build)
		p.Build = &b
	}
	if s.kind == core.JobMeasure {
		p.Samples = s.samples
		p.Benchmark = command(s.bench)
		p.Result = task.Result{JSONArgument: "--out", Field: "ops"}
		if s.linked {
			p.CheckPass = "j-pass"
		}
	}
	return p
}

// verify checks the verdict against what the scenario wants.
func (s scenario) verify(t *testing.T, v flow.Verdict) {
	t.Helper()
	if v.Passed() != s.passed {
		t.Fatalf("passed = %v, want %v: %+v", v.Passed(), s.passed, v)
	}
	if s.passed {
		f := v.Findings
		if s.kind == core.JobMeasure {
			if len(f.Samples) != int(s.samples) {
				t.Errorf("samples = %v, want %d of them", f.Samples, s.samples)
			}
			for _, x := range f.Samples {
				if x != 7.5 {
					t.Errorf("a sample is %v, want 7.5", x)
				}
			}
		} else if len(f.Samples) != 0 {
			t.Errorf("a check job has samples %v", f.Samples)
		}
		if f.Machine != facts {
			t.Errorf("machine = %+v, want the backend's %+v", f.Machine, facts)
		}
		if f.Load.SampledAt.IsZero() {
			t.Errorf("the load was not sampled")
		}
		return
	}
	if v.Stage != s.stage {
		t.Errorf("stage = %q, want %q (%+v)", v.Stage, s.stage, v)
	}
	if s.exec != "" && (v.Exec.Kind != s.exec || v.Exec.ExitCode != s.exitCode) {
		t.Errorf("exec = %+v, want %s with code %d", v.Exec, s.exec, s.exitCode)
	}
	if s.rejected != "" && !strings.Contains(v.Rejected, s.rejected) {
		t.Errorf("rejected = %q, want it to say %q", v.Rejected, s.rejected)
	}
	if v.Reason == "" {
		t.Errorf("a job that did not pass says nothing of why")
	}
}

// lineWatch is the job's output as the test sees it: it is signalled when a line says marker.
type lineWatch struct {
	mu   sync.Mutex
	buf  strings.Builder
	seen chan struct{}
	once sync.Once
}

func newLineWatch() *lineWatch { return &lineWatch{seen: make(chan struct{})} }

func (w *lineWatch) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.buf.Write(p)
	for _, line := range strings.Split(w.buf.String(), "\n") {
		if line == marker {
			w.once.Do(func() { close(w.seen) })
		}
	}
	return len(p), nil
}

func (w *lineWatch) String() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.buf.String()
}

// awaitMarker waits until the command says it runs.
func awaitMarker(t *testing.T, w *lineWatch) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), backstop)
	defer cancel()
	select {
	case <-w.seen:
	case <-ctx.Done():
		t.Fatalf("the command never said it runs; output %q", w.String())
	}
}

// stageHangs are the jobs that hang in one stage, to cancel there.
func stageHangs() []scenario {
	ok := exits(0)
	base := scenario{kind: core.JobMeasure, build: ptr(ok), check: ok, bench: writes(goodJSON, 0), samples: 2, exec: core.ExecCancelled}
	var out []scenario
	for _, stage := range []flow.Stage{flow.StageBuild, flow.StageCheck, flow.StageBenchmark} {
		s := base
		s.name = "cancelled in the " + string(stage)
		s.stage = stage
		switch stage {
		case flow.StageBuild:
			s.build = ptr(hangs())
		case flow.StageCheck:
			s.check = hangs()
		case flow.StageBenchmark:
			s.bench = hangs()
		}
		out = append(out, s)
	}
	return out
}
