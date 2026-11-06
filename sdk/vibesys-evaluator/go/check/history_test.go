package check_test

import (
	"bytes"
	"compress/gzip"
	"fmt"
	"math/rand"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protodelim"
	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/check"
	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/files"
)

// writeHistory writes phases and faults as the accuracy capture does (docs/spec.md, "Accuracy
// history"): history.binpb, then per phase loadgen's results and summary and the generator's
// kept records. legacy leaves out the manifest and puts the kept records in state/, as a phase
// pod does today. A call with Client -1 has no client recorded; notSent adds, per phase, an
// operation loadgen never started.
func writeHistory(t testing.TB, dir string, phases [][]check.Call, faults []check.Fault, legacy bool) {
	t.Helper()
	names := []string{"read", "write"}
	for i, calls := range phases {
		pdir := filepath.Join(dir, fmt.Sprintf("phase-%d", i))
		keptDir := pdir
		if legacy {
			keptDir = filepath.Join(pdir, "state")
		}
		must(t, os.MkdirAll(keptDir, 0o755))
		var results, summary, kept bytes.Buffer
		rw, err := files.NewWriter(&results, files.Binpb, files.Header(pb.FileKind_FILE_KIND_RESULTS, "test"))
		must(t, err)
		for _, c := range calls {
			op := uint32(0)
			if c.Op == "write" {
				op = 1
			}
			res := &pb.OperationResult{Index: c.Index, Operation: op, ScheduledUnixNanos: c.Start, StartUnixNanos: c.Start, EndUnixNanos: c.End}
			if c.Client >= 0 {
				res.Client = proto.Uint32(uint32(c.Client))
			}
			for _, s := range c.Steps {
				step := &pb.StepResult{StartUnixNanos: s.Start, EndUnixNanos: s.End, Status: uint32(s.Status), ErrorMessage: s.Error}
				if s.Status == 0 {
					step.Error = pb.ErrorKind_ERROR_KIND_TIMEOUT
				} else {
					step.Response = &pb.StepResult_Body{Body: s.Body}
				}
				res.Steps = append(res.Steps, step)
			}
			must(t, rw.Write(res))
			_, err := protodelim.MarshalTo(&kept, &pb.KeptOperation{Index: c.Index, Operation: op, InfoJson: string(c.Info), Sampled: true})
			must(t, err)
		}
		// An operation due in the window but never sent: not in the history.
		must(t, rw.Write(&pb.OperationResult{Index: 1 << 40, Error: pb.ErrorKind_ERROR_KIND_CANCELLED}))
		must(t, rw.Flush())
		sw, err := files.NewWriter(&summary, files.Binpb, files.Header(pb.FileKind_FILE_KIND_SUMMARY, "test"))
		must(t, err)
		must(t, sw.Write(&pb.PhaseSummary{OperationNames: names}))
		must(t, sw.Flush())
		must(t, os.WriteFile(filepath.Join(pdir, "results.binpb"), results.Bytes(), 0o644))
		must(t, os.WriteFile(filepath.Join(pdir, "summary.binpb"), summary.Bytes(), 0o644))
		must(t, os.WriteFile(filepath.Join(keptDir, "kept.binpb"), kept.Bytes(), 0o644))
	}
	must(t, os.WriteFile(filepath.Join(dir, "dataset.json"), []byte("{}"), 0o644))
	if legacy {
		return
	}
	m := &pb.HistoryManifest{Phases: uint32(len(phases)), Dataset: "dataset.json"}
	for _, f := range faults {
		m.Faults = append(m.Faults, &pb.FaultEvent{Kind: f.Kind, Target: f.Target, StartUnixNanos: f.Start, EndUnixNanos: f.End, AfterPhase: uint32(f.AfterPhase)})
	}
	raw, err := proto.Marshal(m)
	must(t, err)
	must(t, os.WriteFile(filepath.Join(dir, "history.binpb"), raw, 0o644))
}

func must(t testing.TB, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

// randomCalls is a phase of random kv calls: some multi-step, some lost, some without a client.
func randomCalls(rng *rand.Rand, phase int, n int) []check.Call {
	var calls []check.Call
	t0 := int64(phase)*1e9 + 1 // start 0 means never sent
	for i := range n {
		write := rng.Intn(2) == 0
		client := rng.Intn(4)
		if rng.Intn(5) == 0 {
			client = -1
		}
		c := kvCall(uint64(i), client, t0, t0+int64(1+rng.Intn(1000)), write, []string{fmt.Sprintf("k%d", rng.Intn(3))}, []int{rng.Intn(9)}, rng.Intn(7) == 0)
		c.Phase = phase
		if rng.Intn(4) == 0 { // a login step first
			login := check.Step{Start: c.Start, End: c.Start, Status: 200, Body: []byte(`{"token":"t"}`)}
			c.Steps = append([]check.Step{login}, c.Steps...)
		}
		calls = append(calls, c)
		t0 += int64(rng.Intn(2000))
	}
	return calls
}

// What the capture writes is what OpenHistory reads, call for call (operations never sent are
// left out), with and without the manifest.
func TestOpenHistoryReadsWhatTheCaptureWrites(t *testing.T) {
	for seed := range int64(20) {
		rng := rand.New(rand.NewSource(seed))
		phases := [][]check.Call{randomCalls(rng, 0, 30), randomCalls(rng, 1, 30)}
		faults := []check.Fault{{Kind: "restart", Target: "app", Start: 5e8, End: 6e8, AfterPhase: 0}}
		for _, legacy := range []bool{false, true} {
			dir := t.TempDir()
			writeHistory(t, dir, phases, faults, legacy)
			h, err := check.OpenHistory(dir)
			must(t, err)
			if h.Phases != 2 || h.Dataset != filepath.Join(dir, "dataset.json") {
				t.Fatalf("phases %d, dataset %q", h.Phases, h.Dataset)
			}
			if want := faults; legacy {
				if h.Faults != nil {
					t.Fatalf("a legacy history has no faults, got %v", h.Faults)
				}
			} else if !reflect.DeepEqual(h.Faults, want) {
				t.Fatalf("faults %v, want %v", h.Faults, want)
			}
			for i, want := range phases {
				got, err := h.Phase(i)
				must(t, err)
				if len(got) != len(want) {
					t.Fatalf("phase %d: %d calls, want %d", i, len(got), len(want))
				}
				for j := range want {
					w := want[j]
					for k := range w.Steps {
						if w.Steps[k].Status == 0 {
							w.Steps[k].Error = "timeout"
						}
					}
					if !reflect.DeepEqual(got[j], w) {
						t.Fatalf("seed %d phase %d call %d:\n got %+v\nwant %+v", seed, i, j, got[j], w)
					}
					if got[j].Lost() != w.Lost() {
						t.Fatalf("Lost differs")
					}
				}
			}
		}
	}
}

func TestOpenHistoryRejectsIncompleteDirectories(t *testing.T) {
	dir := t.TempDir()
	if _, err := check.OpenHistory(dir); err == nil {
		t.Fatal("an empty directory opened")
	}
	writeHistory(t, dir, [][]check.Call{{w(0, 1, 10, "a", 1)}}, nil, false)
	must(t, os.Remove(filepath.Join(dir, "phase-0", "results.binpb")))
	if _, err := check.OpenHistory(dir); err == nil || !strings.Contains(err.Error(), "phase 0") {
		t.Fatalf("a missing results file: %v", err)
	}
}

// gzipResults replaces a phase directory's results.binpb with results.binpb.gz, as a phase's
// artifacts keep it.
func gzipResults(t *testing.T, phase string) {
	raw, err := os.ReadFile(filepath.Join(phase, "results.binpb"))
	must(t, err)
	var packed bytes.Buffer
	z := gzip.NewWriter(&packed)
	_, err = z.Write(raw)
	must(t, err)
	must(t, z.Close())
	must(t, os.WriteFile(filepath.Join(phase, "results.binpb.gz"), packed.Bytes(), 0o644))
	must(t, os.Remove(filepath.Join(phase, "results.binpb")))
}

// WriteHistory makes a history directory of loadgen phase directories as a phase pod leaves
// them (kept records in state/, results gzipped or not) and a fault list: a fault without times
// is placed between the phases.
func TestWriteHistoryImportsPhaseDirectories(t *testing.T) {
	rng := rand.New(rand.NewSource(1))
	phases := [][]check.Call{randomCalls(rng, 0, 30), randomCalls(rng, 1, 30), randomCalls(rng, 2, 30)}
	src := t.TempDir()
	writeHistory(t, src, phases, nil, true)
	gzipResults(t, filepath.Join(src, "phase-1"))
	dirs := []string{filepath.Join(src, "phase-0"), filepath.Join(src, "phase-1"), filepath.Join(src, "phase-2")}
	faults := []check.Fault{
		{Kind: "restart", Target: "app", AfterPhase: 0},
		{Kind: "flush", Target: "cache", Start: 15e8, AfterPhase: 1},
	}
	dst := filepath.Join(t.TempDir(), "history")
	must(t, check.WriteHistory(dst, dirs, faults, filepath.Join(src, "dataset.json")))
	h, err := check.OpenHistory(dst)
	must(t, err)
	if h.Phases != 3 || h.Dataset != filepath.Join(dst, "dataset.json") {
		t.Fatalf("phases %d, dataset %q", h.Phases, h.Dataset)
	}
	var lastEnd0, firstStart1 int64 = 0, 1 << 62
	for _, c := range phases[0] {
		lastEnd0 = max(lastEnd0, c.End)
	}
	for _, c := range phases[1] {
		firstStart1 = min(firstStart1, c.Start)
	}
	want := []check.Fault{
		{Kind: "restart", Target: "app", Start: lastEnd0 + 1, End: firstStart1 - 1, AfterPhase: 0},
		{Kind: "flush", Target: "cache", Start: 15e8, End: 15e8, AfterPhase: 1},
	}
	if !reflect.DeepEqual(h.Faults, want) {
		t.Fatalf("faults %+v, want %+v", h.Faults, want)
	}
	for i := range phases {
		got, err := h.Phase(i)
		must(t, err)
		if len(got) != len(phases[i]) {
			t.Fatalf("phase %d: %d calls, want %d", i, len(got), len(phases[i]))
		}
	}
	for _, bad := range []struct {
		name   string
		dir    string
		faults []check.Fault
	}{
		{"a directory that is not empty", dst, nil},
		{"a kind the manifest refuses", "", []check.Fault{{Kind: "Restart", AfterPhase: 0}}},
		{"a fault after no phase", "", []check.Fault{{Kind: "restart", AfterPhase: 3}}},
		{"a fault that ends before it starts", "", []check.Fault{{Kind: "restart", Start: 9, End: 8}}},
	} {
		dir := bad.dir
		if dir == "" {
			dir = filepath.Join(t.TempDir(), "h")
		}
		if err := check.WriteHistory(dir, dirs, bad.faults, ""); err == nil {
			t.Fatalf("%s: written", bad.name)
		}
	}
}

// oracle runs Main in a subprocess (this test binary, as TestOracleSubprocess) with args and
// returns its exit code and output.
func oracle(t *testing.T, args ...string) (code int, stdout, stderr string) {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-test.run=^TestOracleSubprocess$")
	cmd.Env = append(os.Environ(), "CHECK_TEST_ARGS="+strings.Join(args, "\x1f"))
	var out, errOut bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errOut
	err := cmd.Run()
	if e, ok := err.(*exec.ExitError); ok {
		code = e.ExitCode()
	} else if err != nil {
		t.Fatal(err)
	}
	return code, out.String(), errOut.String()
}

// TestOracleSubprocess is the subprocess oracle runs: a kv oracle on check.Main.
func TestOracleSubprocess(t *testing.T) {
	args := os.Getenv("CHECK_TEST_ARGS")
	if args == "" {
		t.Skip("the subprocess of oracle")
	}
	os.Args = append([]string{"oracle"}, strings.Split(args, "\x1f")...)
	check.Main(func(h *check.History) ([]check.AnySpec, error) {
		if h.Dataset == "" {
			return nil, fmt.Errorf("no dataset")
		}
		return []check.AnySpec{kvSpec("kv", false)}, nil
	})
}

// Main's import command writes a history Main then checks.
func TestMainImportsPhaseDirectories(t *testing.T) {
	src, dst := t.TempDir(), filepath.Join(t.TempDir(), "history")
	writeHistory(t, src, [][]check.Call{{w(0, 1, 10, "a", 1)}, {r(0, 100, 110, "a", 1)}}, nil, true)
	code, stdout, stderr := oracle(t, "import", "--out", dst, "--dataset", filepath.Join(src, "dataset.json"),
		"--fault", "kind=restart,target=app,after=0", filepath.Join(src, "phase-0"), filepath.Join(src, "phase-1"))
	if code != 0 || !strings.Contains(stdout, "2 phases, 1 faults") {
		t.Fatalf("import: exit %d\nstdout:\n%s\nstderr:\n%s", code, stdout, stderr)
	}
	if code, stdout, stderr := oracle(t, "--history", dst, "--out", t.TempDir()); code != 0 || !strings.Contains(stdout, "1 faults") {
		t.Fatalf("check: exit %d\nstdout:\n%s\nstderr:\n%s", code, stdout, stderr)
	}
	if code, _, stderr := oracle(t, "import", "--out", filepath.Join(t.TempDir(), "h"), "--fault", "kind=restart", filepath.Join(src, "phase-0")); code != 2 || !strings.Contains(stderr, "after") {
		t.Fatalf("a fault without after: exit %d, %s", code, stderr)
	}
}

// Main is the oracle's command line: exit 0 PASS, 1 FAIL, 2 UNKNOWN, the reason on standard
// error when it is not PASS, the report and timelines in --out. One subprocess per verdict
// proves the wiring (the logic is Check's, tested above).
func TestMainExitsWithTheVerdict(t *testing.T) {
	pass := [][]check.Call{{w(0, 1, 10, "a", 1), r(1, 20, 30, "a", 1)}}
	fail := [][]check.Call{{w(0, 1, 10, "a,b", 1, 1), r(1, 5, 30, "a,b", 1, 0)}}
	for _, c := range []struct {
		name   string
		phases [][]check.Call
		code   int
		files  int
	}{{"pass", pass, 0, 1}, {"fail", fail, 1, 2}, {"unreadable", nil, 2, 0}} {
		t.Run(c.name, func(t *testing.T) {
			dir, out := t.TempDir(), filepath.Join(t.TempDir(), "accuracy")
			if c.phases != nil {
				writeHistory(t, dir, c.phases, nil, false)
			}
			code, stdout, stderr := oracle(t, "--history", dir, "--out", out)
			if code != c.code {
				t.Fatalf("exit %d, want %d\nstdout:\n%s\nstderr:\n%s", code, c.code, stdout, stderr)
			}
			if (c.code != 0) != (strings.TrimSpace(stderr) != "") {
				t.Fatalf("stderr %q for exit %d", stderr, code)
			}
			written, _ := filepath.Glob(filepath.Join(out, "*"))
			if len(written) != c.files {
				t.Fatalf("wrote %v, want %d files", written, c.files)
			}
		})
	}
}
