package cpu_test

import (
	"context"
	"fmt"
	"math/rand"
	"runtime"
	"strings"
	"testing"
	"testing/fstest"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend"
	"github.com/uw-syfi/vibesys/cli/internal/backend/backendtest"
	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

var facts = core.MachineFacts{HostID: "host-a", OS: "linux", Arch: "amd64", CPUModel: "Test CPU", LogicalCPUs: 8, MemoryBytes: 16 << 30}

func fakeBackend(t *testing.T) backend.Backend {
	sys := cpu.NewFakeSystem(facts)
	sys.Queue(cpu.Reading{LoadAverage1m: 0.5, CPU: cpu.Counters{Busy: 100, Total: 1000}, MemoryAvailableBytes: 8 << 30})
	return cpu.New(sys, clock.NewManual(time.Unix(1, 0), time.Millisecond))
}

func TestTheFakeBackendPassesTheContract(t *testing.T) { backendtest.Run(t, fakeBackend) }

func TestTheMachineBackendPassesTheContract(t *testing.T) {
	backendtest.Run(t, func(t *testing.T) backend.Backend {
		return cpu.New(cpu.Host(), clock.System{})
	})
}

func TestMachineIsReadOnce(t *testing.T) {
	sys := cpu.NewFakeSystem(facts)
	b := cpu.New(sys, clock.System{})
	for i := 0; i < 3; i++ {
		got, err := b.Machine(context.Background())
		if err != nil || got != facts {
			t.Fatalf("Machine() = %+v, %v", got, err)
		}
	}
	if n := sys.MachineCalls(); n != 1 {
		t.Errorf("the machine was read %d times, want once", n)
	}
}

func TestAFailedMachineReadIsNotRemembered(t *testing.T) {
	sys := cpu.NewFakeSystem(facts)
	sys.FailWith(&core.Failure{Kind: core.FailureUnavailable, Message: "no proc"})
	b := cpu.New(sys, clock.System{})
	if _, err := b.Machine(context.Background()); err == nil {
		t.Fatal("Machine succeeded on a machine that cannot be read")
	}
	sys.FailWith(nil)
	if got, err := b.Machine(context.Background()); err != nil || got != facts {
		t.Errorf("Machine() after recovery = %+v, %v", got, err)
	}
}

// The busy fraction between two samples is the busy time over the elapsed time between their
// counters, whatever the counters are.
func TestBusyFractionIsTheShareOfTheIntervalSpentBusy(t *testing.T) {
	rng := rand.New(rand.NewSource(1))
	for i := 0; i < 500; i++ {
		prev := cpu.Counters{Busy: uint64(rng.Int63n(1 << 40))}
		prev.Total = prev.Busy + uint64(rng.Int63n(1<<40))
		dBusy := uint64(rng.Int63n(1 << 30))
		dIdle := uint64(rng.Int63n(1 << 30))
		now := cpu.Counters{Busy: prev.Busy + dBusy, Total: prev.Total + dBusy + dIdle}
		got := cpu.BusyFraction(&prev, now)
		if got < 0 || got > 1 {
			t.Fatalf("BusyFraction(%+v, %+v) = %v, outside [0, 1]", prev, now, got)
		}
		if dBusy+dIdle > 0 {
			want := float64(dBusy) / float64(dBusy+dIdle)
			if d := got - want; d > 1e-12 || d < -1e-12 {
				t.Fatalf("BusyFraction(%+v, %+v) = %v, want %v", prev, now, got, want)
			}
		}
	}
}

func TestBusyFractionOfCountersThatDidNotMoveForwardIsTheShareSinceTheStart(t *testing.T) {
	now := cpu.Counters{Busy: 30, Total: 120}
	for name, prev := range map[string]*cpu.Counters{
		"no earlier reading": nil,
		"unchanged":          {Busy: 30, Total: 120},
		"reset":              {Busy: 90, Total: 400},
		"busy went back":     {Busy: 40, Total: 100},
	} {
		if got := cpu.BusyFraction(prev, now); got != 0.25 {
			t.Errorf("%s: BusyFraction = %v, want 0.25", name, got)
		}
	}
	if got := cpu.BusyFraction(nil, cpu.Counters{}); got != 0 {
		t.Errorf("BusyFraction of no time = %v, want 0", got)
	}
}

func TestLoadCoversTheTimeSinceThePreviousLoad(t *testing.T) {
	sys := cpu.NewFakeSystem(facts)
	sys.Queue(
		cpu.Reading{CPU: cpu.Counters{Busy: 10, Total: 100}},
		cpu.Reading{CPU: cpu.Counters{Busy: 60, Total: 200}},
		cpu.Reading{CPU: cpu.Counters{Busy: 60, Total: 300}},
	)
	b := cpu.New(sys, clock.NewManual(time.Unix(1, 0), time.Second))
	var got []float64
	for i := 0; i < 3; i++ {
		l, err := b.Load(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		got = append(got, l.CPUBusyFraction)
	}
	if want := []float64{0.1, 0.5, 0}; fmt.Sprint(got) != fmt.Sprint(want) {
		t.Errorf("busy fractions = %v, want %v", got, want)
	}
}

func TestLoadIsStampedWithTheClockAndCarriesTheReading(t *testing.T) {
	sys := cpu.NewFakeSystem(facts)
	sys.Queue(cpu.Reading{LoadAverage1m: 2.5, MemoryAvailableBytes: 123})
	at := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	l, err := cpu.New(sys, clock.NewManual(at, 0)).Load(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !l.SampledAt.Equal(at) || l.LoadAverage1m != 2.5 || l.MemoryAvailableBytes != 123 {
		t.Errorf("Load() = %+v", l)
	}
}

func TestAUnreadableMachineIsUnavailable(t *testing.T) {
	sys := cpu.NewFakeSystem(facts)
	want := &core.Failure{Kind: core.FailureUnavailable, Message: "no proc"}
	sys.FailWith(want)
	if _, err := cpu.New(sys, clock.System{}).Load(context.Background()); err != want {
		t.Errorf("Load error = %v, want %v", err, want)
	}
}

const cpuinfo = `processor	: 0
vendor_id	: GenuineIntel
model name	: Intel(R) Xeon(R)   CPU @ 2.20GHz
cpu MHz		: 2200.000

processor	: 1
model name	: Intel(R) Xeon(R) CPU @ 2.20GHz
`

func TestParseCPUInfo(t *testing.T) {
	model, n := cpu.ParseCPUInfo(cpuinfo)
	if model != "Intel(R) Xeon(R) CPU @ 2.20GHz" || n != 2 {
		t.Errorf("ParseCPUInfo = %q, %d", model, n)
	}
	if model, n := cpu.ParseCPUInfo("Hardware\t: BCM2711\nprocessor : 0\n"); model != "BCM2711" || n != 1 {
		t.Errorf("ParseCPUInfo of an arm board = %q, %d", model, n)
	}
	if model, n := cpu.ParseCPUInfo(""); model != "" || n != 0 {
		t.Errorf("ParseCPUInfo of nothing = %q, %d", model, n)
	}
}

func TestParseMemInfoReadsWhatTheKernelWrites(t *testing.T) {
	rng := rand.New(rand.NewSource(2))
	for i := 0; i < 200; i++ {
		total := uint64(rng.Int63n(1<<40)) + 1
		avail := uint64(rng.Int63n(int64(total)))
		text := fmt.Sprintf("MemTotal:       %d kB\nMemFree:        %d kB\nMemAvailable:   %d kB\nBuffers: 5 kB\n", total, avail/2, avail)
		gotTotal, gotAvail, ok := cpu.ParseMemInfo(text)
		if !ok || gotTotal != total*1024 || gotAvail != avail*1024 {
			t.Fatalf("ParseMemInfo(%q) = %d, %d, %v", text, gotTotal, gotAvail, ok)
		}
	}
}

func TestParseMemInfoWithoutMemAvailableUsesFreeMemoryAndNeverExceedsTheTotal(t *testing.T) {
	total, avail, ok := cpu.ParseMemInfo("MemTotal: 1000 kB\nMemFree: 400 kB\n")
	if !ok || total != 1000*1024 || avail != 400*1024 {
		t.Errorf("ParseMemInfo = %d, %d, %v", total, avail, ok)
	}
	if _, avail, _ := cpu.ParseMemInfo("MemTotal: 10 kB\nMemAvailable: 99 kB\n"); avail != 10*1024 {
		t.Errorf("available %d exceeds the total", avail)
	}
	if _, _, ok := cpu.ParseMemInfo("MemFree: 4 kB\n"); ok {
		t.Error("ParseMemInfo accepted a meminfo with no total")
	}
}

func TestParseLoadAvg(t *testing.T) {
	if got, err := cpu.ParseLoadAvg("0.52 0.58 0.59 1/467 12345\n"); err != nil || got != 0.52 {
		t.Errorf("ParseLoadAvg = %v, %v", got, err)
	}
	for _, bad := range []string{"", "x 1 2", "NaN 1 2", "-1 0 0", "Inf 0 0"} {
		if _, err := cpu.ParseLoadAvg(bad); err == nil {
			t.Errorf("ParseLoadAvg(%q) succeeded", bad)
		}
	}
}

// Busy time is every mode but idle and waiting, and the total adds those two, whatever the
// kernel counted in each.
func TestParseStatSplitsBusyFromIdle(t *testing.T) {
	rng := rand.New(rand.NewSource(3))
	for i := 0; i < 200; i++ {
		var t8 [10]uint64
		for j := range t8 {
			t8[j] = uint64(rng.Int63n(1 << 30))
		}
		line := "cpu "
		for _, v := range t8 {
			line += fmt.Sprintf(" %d", v)
		}
		text := line + "\ncpu0 1 2 3 4 5 6 7 8 9 10\nintr 99\n"
		got, err := cpu.ParseStat(text)
		busy := t8[0] + t8[1] + t8[2] + t8[5] + t8[6] + t8[7]
		if err != nil || got.Busy != busy || got.Total != busy+t8[3]+t8[4] {
			t.Fatalf("ParseStat(%q) = %+v, %v; want busy %d total %d", text, got, err, busy, busy+t8[3]+t8[4])
		}
	}
	for _, bad := range []string{"", "intr 1\n", "cpu 1 2\n", "cpu a b c d\n"} {
		if _, err := cpu.ParseStat(bad); err == nil {
			t.Errorf("ParseStat(%q) succeeded", bad)
		}
	}
}

func procFS(files map[string]string) fstest.MapFS {
	fsys := fstest.MapFS{}
	for name, text := range files {
		fsys[name] = &fstest.MapFile{Data: []byte(text)}
	}
	return fsys
}

func newProc(files map[string]string) *cpu.Proc {
	return &cpu.Proc{
		FS: procFS(files), Hostname: func() (string, error) { return "host-a", nil },
		OS: runtime.GOOS, Arch: runtime.GOARCH, Processors: 4,
	}
}

var goodProc = map[string]string{
	"cpuinfo": cpuinfo, "meminfo": "MemTotal: 2048 kB\nMemAvailable: 1024 kB\n",
	"loadavg": "1.5 1 1 1/1 1\n", "stat": "cpu 100 0 100 700 100 0 0 0\n",
}

func TestProcReadsTheMachineAndItsLoad(t *testing.T) {
	p := newProc(goodProc)
	m, err := p.Machine(context.Background())
	want := core.MachineFacts{HostID: "host-a", OS: runtime.GOOS, Arch: runtime.GOARCH, CPUModel: "Intel(R) Xeon(R) CPU @ 2.20GHz", LogicalCPUs: 2, MemoryBytes: 2048 * 1024}
	if err != nil || m != want {
		t.Fatalf("Machine() = %+v, %v; want %+v", m, err, want)
	}
	r, err := p.Read(context.Background())
	if err != nil || r.LoadAverage1m != 1.5 || r.CPU != (cpu.Counters{Busy: 200, Total: 1000}) || r.MemoryAvailableBytes != 1024*1024 {
		t.Errorf("Read() = %+v, %v", r, err)
	}
}

func TestProcFallsBackOnTheProcessorCountButNotOnMissingFiles(t *testing.T) {
	files := map[string]string{"cpuinfo": "model name : X\n", "meminfo": goodProc["meminfo"]}
	m, err := newProc(files).Machine(context.Background())
	if err != nil || m.LogicalCPUs != 4 {
		t.Errorf("Machine() = %+v, %v; want the 4 logical CPUs the runtime reports", m, err)
	}
	for _, missing := range []string{"cpuinfo", "meminfo"} {
		files := map[string]string{}
		for k, v := range goodProc {
			if k != missing {
				files[k] = v
			}
		}
		_, err := newProc(files).Machine(context.Background())
		var f *core.Failure
		if err == nil || !errorsAs(err, &f) || f.Kind != core.FailureUnavailable || !strings.Contains(f.Message, missing) {
			t.Errorf("Machine without %s: %v, want an unavailable failure naming it", missing, err)
		}
	}
	for _, missing := range []string{"loadavg", "stat", "meminfo"} {
		files := map[string]string{}
		for k, v := range goodProc {
			if k != missing {
				files[k] = v
			}
		}
		if _, err := newProc(files).Read(context.Background()); err == nil {
			t.Errorf("Read without %s succeeded", missing)
		}
	}
}

func errorsAs(err error, target **core.Failure) bool {
	f, ok := err.(*core.Failure)
	if ok {
		*target = f
	}
	return ok
}
