package machinestats_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/bits"
	"math/rand/v2"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"testing/quick"
	"testing/synctest"
	"time"

	"buf.build/go/protovalidate"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/clock"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"github.com/uw-syfi/vibesys/gateway/internal/machinestats"
	"github.com/uw-syfi/vibesys/gateway/internal/machinestats/machinestatstest"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription/subscriptiontest"
	"google.golang.org/protobuf/proto"
)

var request = &apiv1.SubscribeRequest{Topic: &apiv1.SubscribeRequest_MachineStats{MachineStats: &apiv1.MachineStatsTopic{}}}

type tool = func(context.Context, []string) ([]byte, error)

func sample(t *testing.T, goos string, host machinestats.Host) *apiv1.MachineStats {
	t.Helper()
	sampler, err := machinestats.NewSampler(goos, clock.System{}, host)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), machinestats.SampleDeadline)
	defer cancel()
	stats, err := sampler.Sample(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := protovalidate.Validate(stats); err != nil {
		t.Fatalf("%v: %v", stats, err)
	}
	return stats
}

func gpuReason(stats *apiv1.MachineStats) apiv1.AbsenceReason {
	return stats.GetGpuUnavailable().GetReason()
}

func TestLinuxRecordingReadsAsTheMachine(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		start := time.Now()
		stats := sample(t, "linux", machinestatstest.Linux())
		if elapsed := time.Since(start); elapsed != time.Second {
			t.Fatalf("first sample took %s", elapsed)
		}
		if percent := stats.GetCpuStats().GetUtilizationPercent(); percent <= 0 || percent >= 100 {
			t.Fatalf("cpu %v", stats.Cpu)
		}
		memory := stats.GetMemoryStats()
		if memory.GetTotalBytes() == 0 || memory.GetTotalBytes()%1024 != 0 || memory.GetUsedBytes() >= memory.GetTotalBytes() {
			t.Fatalf("memory %v", memory)
		}
		if gpuReason(stats) != apiv1.AbsenceReason_ABSENCE_REASON_TOOL_NOT_FOUND {
			t.Fatalf("gpu %v", stats.Gpu)
		}
	})
}

func TestMacOSRecordingReadsAsTheMachine(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		stats := sample(t, "darwin", machinestatstest.MacOS())
		if percent := stats.GetCpuStats().GetUtilizationPercent(); percent != 100-11.64 {
			t.Fatalf("cpu must come from top's last report: %v", percent)
		}
		const total = 38654705664
		memory := stats.GetMemoryStats()
		if memory.GetTotalBytes() != total || memory.GetUsedBytes() == 0 || memory.GetUsedBytes() > total {
			t.Fatalf("memory %v", memory)
		}
		gpus := stats.GetGpus().GetGpus()
		if len(gpus) != 1 || gpus[0].Name != "Apple M3 Pro" || gpus[0].MemoryTotalBytes != total || gpus[0].MemoryUsedBytes == 0 {
			t.Fatalf("gpus %v", stats.Gpu)
		}
	})
}

// Later samples are a delta over the cadence, so they must not wait another window.
func TestLaterLinuxSamplesAreDeltasOfTheLastRead(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		host := machinestatstest.Linux()
		host.Files["/proc/stat"] = [][]byte{
			[]byte("cpu  100 0 0 100 0 0 0 0 0 0\n"),
			[]byte("cpu  100 0 0 200 0 0 0 0 0 0\n"),
			[]byte("cpu  200 0 0 200 0 0 0 0 0 0\n"),
			[]byte("cpu  300 0 0 250 50 0 0 0 0 0\n"),
		}
		sampler, err := machinestats.NewSampler("linux", clock.System{}, host)
		if err != nil {
			t.Fatal(err)
		}
		want := []float64{0, 100, 50}
		for index, percent := range want {
			stats, err := sampler.Sample(context.Background())
			if err != nil {
				t.Fatal(err)
			}
			if got := stats.GetCpuStats().GetUtilizationPercent(); got != percent {
				t.Fatalf("sample %d: %v, want %v", index, stats.Cpu, percent)
			}
		}
	})
}

// The kernel documents that iowait can decrease, and an unsigned delta would
// report it as a huge idle time.
func TestACounterGoingBackwardsIsClampedNotWrapped(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		host := machinestatstest.Linux()
		host.Files["/proc/stat"] = [][]byte{
			[]byte("cpu  100 0 0 100 900 0 0 0 0 0\n"),
			[]byte("cpu  200 0 0 300 100 0 0 0 0 0\n"),
		}
		got := sample(t, "linux", host).GetCpuStats().GetUtilizationPercent()
		if want := 100.0 / 3; got != want {
			t.Fatalf("utilization %v, want %v", got, want)
		}
	})
}

// Sums of hostile counters wrap, which must not produce a percentage over 100.
func TestCountersThatWrapTheirSumAreAnAbsence(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		host := machinestatstest.Linux()
		host.Files["/proc/stat"] = [][]byte{
			[]byte("cpu  0 0 0 0 0 0 0 0 0 0\n"),
			[]byte("cpu  2 0 0 18446744073709551615 0 0 0 0 0 0\n"),
		}
		if stats := sample(t, "linux", host); stats.GetCpuUnavailable() == nil {
			t.Fatalf("cpu %v", stats.Cpu)
		}
	})
}

func TestNoElapsedCpuTimeIsAnAbsenceNotZero(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		host := machinestatstest.Linux()
		host.Files["/proc/stat"] = host.Files["/proc/stat"][:1]
		stats := sample(t, "linux", host)
		if stats.GetCpuUnavailable().GetReason() != apiv1.AbsenceReason_ABSENCE_REASON_READ_FAILED {
			t.Fatalf("cpu %v", stats.Cpu)
		}
	})
}

func TestGpuToolsReportTypedAbsences(t *testing.T) {
	reply := func(output string, err error) tool {
		return func(context.Context, []string) ([]byte, error) { return []byte(output), err }
	}
	for name, each := range map[string]struct {
		tools map[string]tool
		want  apiv1.AbsenceReason
	}{
		"no tool": {map[string]tool{}, apiv1.AbsenceReason_ABSENCE_REASON_TOOL_NOT_FOUND},
		"rocm-smi on PATH is unverified": {
			map[string]tool{"rocm-smi": reply("", nil)}, apiv1.AbsenceReason_ABSENCE_REASON_TOOL_UNVERIFIED,
		},
		"rocm-smi off PATH is found and unverified": {
			map[string]tool{"/opt/rocm/bin/rocm-smi": reply("", nil)}, apiv1.AbsenceReason_ABSENCE_REASON_TOOL_UNVERIFIED,
		},
		"nvidia-smi fails": {
			map[string]tool{"nvidia-smi": reply("", errors.New("driver not loaded"))}, apiv1.AbsenceReason_ABSENCE_REASON_READ_FAILED,
		},
		"nvidia-smi output is unreadable": {
			map[string]tool{"/usr/bin/nvidia-smi": reply("not a table\n", nil)}, apiv1.AbsenceReason_ABSENCE_REASON_PARSE_FAILED,
		},
		"nvidia-smi wins over rocm-smi": {
			map[string]tool{"nvidia-smi": reply("x\n", nil), "rocm-smi": reply("", nil)}, apiv1.AbsenceReason_ABSENCE_REASON_PARSE_FAILED,
		},
	} {
		t.Run(name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				host := machinestatstest.Linux()
				host.Tools = each.tools
				if got := gpuReason(sample(t, "linux", host)); got != each.want {
					t.Fatalf("got %s, want %s", got, each.want)
				}
			})
		})
	}
}

// A stuck tool costs its own section and the rest of the sample still reports.
func TestAStuckToolTimesOutOnlyItsSection(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		host := machinestatstest.Linux()
		host.Tools["nvidia-smi"] = func(ctx context.Context, _ []string) ([]byte, error) {
			<-ctx.Done()
			return nil, ctx.Err()
		}
		start := time.Now()
		stats := sample(t, "linux", host)
		if elapsed := time.Since(start); elapsed != machinestats.SampleDeadline {
			t.Fatalf("sample took %s", elapsed)
		}
		if gpuReason(stats) != apiv1.AbsenceReason_ABSENCE_REASON_TIMED_OUT || stats.GetCpuStats() == nil || stats.GetMemoryStats() == nil {
			t.Fatalf("stats %v", stats)
		}
	})
}

// sections names the Linux calls of each section, so a test can wedge any subset.
var linuxSections = []struct {
	name  string
	calls []machinestatstest.Call
}{
	{"cpu", []machinestatstest.Call{{Method: "ReadFile", Target: "/proc/stat"}}},
	{"memory", []machinestatstest.Call{{Method: "ReadFile", Target: "/proc/meminfo"}}},
	{"gpu", []machinestatstest.Call{{Method: "Find", Target: "nvidia-smi"}}},
}

func absent(stats *apiv1.MachineStats, section string) *apiv1.Absence {
	switch section {
	case "cpu":
		return stats.GetCpuUnavailable()
	case "memory":
		return stats.GetMemoryUnavailable()
	}
	return stats.GetGpuUnavailable()
}

// wedgedLinux is the recorded machine with a GPU, whose chosen sections block in
// calls that ignore their context.
func wedgedLinux(stuck uint8) *machinestatstest.Wedged {
	host := machinestatstest.Linux()
	host.Files["/proc/stat"] = nil
	for tick := range 32 {
		host.Files["/proc/stat"] = append(host.Files["/proc/stat"], fmt.Appendf(nil, "cpu  %d 0 0 %d 0 0 0 0 0 0\n", 10*tick, 100*tick))
	}
	host.Tools["nvidia-smi"] = func(context.Context, []string) ([]byte, error) {
		return []byte("0, GPU-a, NVIDIA Test, 1024, 40960, 17\n"), nil
	}
	return machinestatstest.Wedge(host, func(call machinestatstest.Call) bool {
		for index, section := range linuxSections {
			if stuck&(1<<index) != 0 && slices.Contains(section.calls, call) {
				return true
			}
		}
		return false
	})
}

// A Host call that never returns costs its own section and one stuck call: the
// sample still ends at its deadline, the other sections still report, and no
// later sample stacks a second call behind the stuck one.
func TestAHostCallThatNeverReturnsIsBoundedToItsSectionAndOneCall(t *testing.T) {
	property := func(stuck uint8, samples uint8) bool {
		stuck %= 1 << len(linuxSections)
		rounds := 2 + int(samples%4)
		ok := true
		synctest.Test(t, func(t *testing.T) {
			host := wedgedLinux(stuck)
			defer close(host.Release)
			sampler, err := machinestats.NewSampler("linux", clock.System{}, host)
			if err != nil {
				t.Fatal(err)
			}
			for round := range rounds {
				ctx, cancel := context.WithTimeout(context.Background(), machinestats.SampleDeadline)
				start := time.Now()
				stats, err := sampler.Sample(ctx)
				cancel()
				if err != nil || time.Since(start) > machinestats.SampleDeadline {
					t.Errorf("round %d: err %v after %s", round, err, time.Since(start))
					ok = false
				}
				if err := protovalidate.Validate(stats); err != nil {
					t.Errorf("round %d: %v", round, err)
					ok = false
				}
				for index, section := range linuxSections {
					got := absent(stats, section.name)
					if wedged := stuck&(1<<index) != 0; wedged != (got != nil) ||
						(wedged && got.GetReason() != apiv1.AbsenceReason_ABSENCE_REASON_TIMED_OUT) {
						t.Errorf("round %d: stuck=%03b, %s reports %v in %v", round, stuck, section.name, got, stats)
						ok = false
					}
				}
				time.Sleep(machinestats.SampleDeadline)
			}
			synctest.Wait()
			if blocked := host.Blocked.Load(); blocked != int64(bits.OnesCount8(stuck)) {
				t.Errorf("stuck=%03b: %d calls blocked over %d samples", stuck, blocked, rounds)
				ok = false
			}
		})
		return ok
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 200}); err != nil {
		t.Fatal(err)
	}
}

// Once the stuck call returns, its section reads again.
func TestASectionRecoversWhenItsStuckCallReturns(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		host := wedgedLinux(0b111)
		sampler, err := machinestats.NewSampler("linux", clock.System{}, host)
		if err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), machinestats.SampleDeadline)
		stats, _ := sampler.Sample(ctx)
		cancel()
		if stats.GetCpuUnavailable() == nil || stats.GetMemoryUnavailable() == nil || stats.GetGpuUnavailable() == nil {
			t.Fatalf("stats %v", stats)
		}
		close(host.Release)
		synctest.Wait()
		for range 2 {
			ctx, cancel = context.WithTimeout(context.Background(), machinestats.SampleDeadline)
			stats, _ = sampler.Sample(ctx)
			cancel()
			time.Sleep(machinestats.SampleDeadline)
		}
		if stats.GetCpuStats() == nil || stats.GetMemoryStats() == nil || stats.GetGpus() == nil {
			t.Fatalf("a section stayed down after its call returned: %v", stats)
		}
	})
}

// The deadline is the topic's: a source that waited for a stuck tool, or for a
// deadline an hour away, would send nothing until then.
func TestTheTopicSendsItsFirstSampleAtTheSampleDeadlineWhateverIsStuck(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		host := wedgedLinux(0b100)
		topic := machinestats.Topic("linux", clock.System{}, host)
		hub := subscription.New(clock.System{}, topic)
		stream := subscriptiontest.Open(hub, 1, request)
		defer func() {
			stream.Stop()
			close(host.Release)
		}()
		time.Sleep(machinestats.SampleDeadline)
		synctest.Wait()
		frames := stream.Frames()
		entry := slices.IndexFunc(frames, func(frame *apiv1.SubscribeResponse) bool { return frame.GetEntry() != nil })
		if entry < 0 || !slices.ContainsFunc(frames, func(frame *apiv1.SubscribeResponse) bool { return frame.GetSynced() != nil }) {
			t.Fatalf("no snapshot at the sample deadline: %v", frames)
		}
		stats := frames[entry].GetEntry().GetMachineStats()
		if stats.GetGpuUnavailable().GetReason() != apiv1.AbsenceReason_ABSENCE_REASON_TIMED_OUT || stats.GetCpuStats() == nil || stats.GetMemoryStats() == nil {
			t.Fatalf("stats %v", stats)
		}
		// Subscribing again and again must not stack calls behind the stuck one.
		for range 50 {
			stream.Stop()
			stream = subscriptiontest.Open(hub, 1, request)
			time.Sleep(machinestats.SampleDeadline)
			synctest.Wait()
		}
		if blocked := host.Blocked.Load(); blocked != 1 {
			t.Fatalf("%d calls blocked over 51 subscriptions", blocked)
		}
	})
}

func TestUnreadableSectionsAreIndependentAbsences(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		host := machinestatstest.Linux()
		delete(host.Files, "/proc/meminfo")
		stats := sample(t, "linux", host)
		if stats.GetMemoryUnavailable().GetReason() != apiv1.AbsenceReason_ABSENCE_REASON_READ_FAILED || stats.GetCpuStats() == nil {
			t.Fatalf("stats %v", stats)
		}
		mac := machinestatstest.MacOS()
		delete(mac.Tools, "/usr/sbin/ioreg")
		stats = sample(t, "darwin", mac)
		if gpuReason(stats) != apiv1.AbsenceReason_ABSENCE_REASON_TOOL_NOT_FOUND || stats.GetMemoryStats() == nil {
			t.Fatalf("stats %v", stats)
		}
	})
}

func TestAPanickingHostIsAnAbsence(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		host := machinestatstest.Linux()
		host.Tools["nvidia-smi"] = func(context.Context, []string) ([]byte, error) { panic("broken") }
		if got := gpuReason(sample(t, "linux", host)); got != apiv1.AbsenceReason_ABSENCE_REASON_PARSE_FAILED {
			t.Fatalf("got %s", got)
		}
	})
}

func TestAnOperatingSystemWithoutASamplerIsUnsupported(t *testing.T) {
	if _, err := machinestats.NewSampler("plan9", clock.System{}, machinestatstest.Linux()); err == nil {
		t.Fatal("plan9 got a sampler")
	}
}

type fixtureGpu struct {
	Index          uint32 `json:"index"`
	Name           string `json:"name"`
	MemoryUsedMib  uint64 `json:"memory_used_mib"`
	MemoryTotalMib uint64 `json:"memory_total_mib"`
	UtilizationPct uint64 `json:"utilization_pct"`
}

// Python's parser reads the same file, so the two cannot drift on the rows it
// holds. The one intended difference, a refused row, is stated per case.
func TestNvidiaOutputMatchesTheSharedFixture(t *testing.T) {
	data, err := os.ReadFile("../../../tests/conformance/fixtures/gpu-telemetry/nvidia-smi.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Query []string `json:"query"`
		Cases []struct {
			Name    string       `json:"name"`
			Output  string       `json:"output"`
			Gpus    []fixtureGpu `json:"gpus"`
			Refused bool         `json:"refused_by_gateway"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatal(err)
	}
	if len(fixture.Cases) == 0 {
		t.Fatal("empty fixture")
	}
	for _, each := range fixture.Cases {
		t.Run(each.Name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				host := machinestatstest.Linux()
				host.Tools["nvidia-smi"] = func(_ context.Context, args []string) ([]byte, error) {
					if !slices.Equal(args, fixture.Query) {
						t.Errorf("query %v, want %v", args, fixture.Query)
					}
					return []byte(each.Output), nil
				}
				stats := sample(t, "linux", host)
				if each.Refused {
					if gpuReason(stats) != apiv1.AbsenceReason_ABSENCE_REASON_PARSE_FAILED {
						t.Fatalf("gpu %v", stats.Gpu)
					}
					return
				}
				got := stats.GetGpus().GetGpus()
				if stats.GetGpus() == nil || len(got) != len(each.Gpus) {
					t.Fatalf("gpu %v, want %v", stats.Gpu, each.Gpus)
				}
				for index, gpu := range each.Gpus {
					const mebibyte = 1 << 20
					if got[index].Index != gpu.Index || got[index].Name != gpu.Name ||
						got[index].MemoryUsedBytes != gpu.MemoryUsedMib*mebibyte ||
						got[index].MemoryTotalBytes != gpu.MemoryTotalMib*mebibyte ||
						got[index].UtilizationPercent != float64(gpu.UtilizationPct) {
						t.Fatalf("gpu %d: %v, want %+v", index, got[index], gpu)
					}
				}
			})
		})
	}
}

// corrupt rewrites a recording at generated points, so the input stays close
// enough to the real format to reach every parser's deep branches.
func corrupt(recording []byte, seed uint64) []byte {
	random := rand.New(rand.NewPCG(seed, 0))
	replacements := []string{"", "x", "-1", "99999999999999999999999999", "1e308", "NaN", ",", "\n", "\xff", ":", "%", "\"", "="}
	output := append([]byte{}, recording...)
	for range 1 + random.IntN(6) {
		if len(output) == 0 {
			break
		}
		at := random.IntN(len(output))
		end := min(len(output), at+random.IntN(16))
		output = append(output[:at:at], append([]byte(replacements[random.IntN(len(replacements))]), output[end:]...)...)
	}
	return output
}

// Whatever a tool prints, a sample is schema-valid: a bad value must become its
// section's absence and never reach the sink, where it would end the topic.
func TestAnyToolOutputYieldsAValidSample(t *testing.T) {
	nvidia := "0, GPU-a, NVIDIA Test, 1024, 40960, 17\n1, GPU-b, NVIDIA Test, 0, 40960, 0\n"
	property := func(goos bool, seed uint64) bool {
		synctest.Test(t, func(t *testing.T) {
			name := "linux"
			host := machinestatstest.Linux()
			if goos {
				name, host = "darwin", machinestatstest.MacOS()
			}
			for path, contents := range host.Files {
				for index := range contents {
					contents[index] = corrupt(contents[index], seed+uint64(index))
				}
				host.Files[path] = contents
			}
			for path, run := range host.Tools {
				host.Tools[path] = func(ctx context.Context, args []string) ([]byte, error) {
					output, err := run(ctx, args)
					return corrupt(output, seed), err
				}
			}
			if !goos {
				host.Tools["nvidia-smi"] = func(context.Context, []string) ([]byte, error) {
					return corrupt([]byte(nvidia), seed), nil
				}
			}
			sample(t, name, host)
		})
		return true
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 3000}); err != nil {
		t.Fatal(err)
	}
}

// nvidiaRows is output of n GPUs whose names are width bytes long.
func nvidiaRows(n int, width int) string {
	var rows strings.Builder
	for index := range n {
		fmt.Fprintf(&rows, "%d, GPU-%d, %s, 1024, 40960, 17\n", index, index, strings.Repeat("g", width))
	}
	return rows.String()
}

// Every row can satisfy the schema while the list as a whole outgrows the topic's
// value bound, and a value over it ends the topic for every subscriber. The
// sampler holds its own output to the bound: a list that does not fit is an
// absence, whatever the tool printed.
func TestAnyToolOutputYieldsAValueThatFitsTheTopic(t *testing.T) {
	bound := hostoperations.Topics[hostoperations.TopicMachineStats].MaxValueBytes
	property := func(rows, width uint8) bool {
		fits := true
		synctest.Test(t, func(t *testing.T) {
			host := machinestatstest.Linux()
			output := nvidiaRows(int(rows), 1+int(width))
			host.Tools["nvidia-smi"] = func(context.Context, []string) ([]byte, error) { return []byte(output), nil }
			stats := sample(t, "linux", host)
			fits = proto.Size(stats) <= bound
		})
		return fits
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 300}); err != nil {
		t.Fatal(err)
	}
	// The reported input, and a fleet that does fit and must not be refused.
	synctest.Test(t, func(t *testing.T) {
		host := machinestatstest.Linux()
		host.Tools["nvidia-smi"] = func(context.Context, []string) ([]byte, error) { return []byte(nvidiaRows(100, 200)), nil }
		if got := gpuReason(sample(t, "linux", host)); got != apiv1.AbsenceReason_ABSENCE_REASON_PARSE_FAILED {
			t.Fatalf("100 GPUs with 200-byte names: %s", got)
		}
		host.Tools["nvidia-smi"] = func(context.Context, []string) ([]byte, error) { return []byte(nvidiaRows(256, 21)), nil }
		if got := sample(t, "linux", host).GetGpus().GetGpus(); len(got) != 256 {
			t.Fatalf("256 GPUs with 21-byte names: %d listed", len(got))
		}
	})
}

// The rules of a section live in the schema and nowhere else: the sampler holds
// each parsed section to them, and a parser copies none. A section that breaks one
// is that section's absence. Sent as it is, it would end the topic for every
// subscriber. Each input below is one a parser alone reads without complaint.
func TestASectionThatBreaksTheSchemasRulesIsAParseFailure(t *testing.T) {
	row := func(name string, used, total, busy int) string {
		return fmt.Sprintf("0, GPU-0, %s, %d, %d, %d\n", name, used, total, busy)
	}
	for name, each := range map[string]struct {
		output string
		listed bool
	}{
		"257 GPUs":                  {nvidiaRows(257, 1), false},
		"256 GPUs":                  {nvidiaRows(256, 1), true},
		"a name of 257 bytes":       {row(strings.Repeat("g", 257), 1, 2, 3), false},
		"a name of 256 bytes":       {row(strings.Repeat("g", 256), 1, 2, 3), true},
		"no name":                   {row("", 1, 2, 3), false},
		"a name of invalid UTF-8":   {row("\xff", 1, 2, 3), false},
		"101 percent busy":          {row("n", 1, 2, 101), false},
		"100 percent busy":          {row("n", 1, 2, 100), true},
		"more memory used than has": {row("n", 3, 2, 3), false},
		"all memory used":           {row("n", 2, 2, 3), true},
		"no memory":                 {row("n", 0, 0, 3), false},
	} {
		t.Run(name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				host := machinestatstest.Linux()
				host.Tools["nvidia-smi"] = func(context.Context, []string) ([]byte, error) { return []byte(each.output), nil }
				stats := sample(t, "linux", host)
				if each.listed != (stats.GetGpus() != nil) || (!each.listed && gpuReason(stats) != apiv1.AbsenceReason_ABSENCE_REASON_PARSE_FAILED) {
					t.Fatalf("listed=%t, want %t: %v", stats.GetGpus() != nil, each.listed, stats.Gpu)
				}
			})
		})
	}
	t.Run("a machine with no memory", func(t *testing.T) {
		synctest.Test(t, func(t *testing.T) {
			host := machinestatstest.Linux()
			host.Files["/proc/meminfo"] = [][]byte{[]byte("MemTotal: 0 kB\nMemAvailable: 0 kB\n")}
			if got := sample(t, "linux", host).GetMemoryUnavailable().GetReason(); got != apiv1.AbsenceReason_ABSENCE_REASON_PARSE_FAILED {
				t.Fatalf("memory %s", got)
			}
		})
	})
	t.Run("a CPU idle for more than all of the time", func(t *testing.T) {
		synctest.Test(t, func(t *testing.T) {
			host := machinestatstest.MacOS()
			host.Tools["/usr/bin/top"] = func(context.Context, []string) ([]byte, error) {
				return []byte("CPU usage: 1.00% user, 1.00% sys, 101.00% idle \n"), nil
			}
			if got := sample(t, "darwin", host).GetCpuUnavailable().GetReason(); got != apiv1.AbsenceReason_ABSENCE_REASON_PARSE_FAILED {
				t.Fatalf("cpu %s", got)
			}
		})
	})
}

// A client that leaves and comes back must not cost the host a sample each time:
// the cadence is the whole rate limit on a shared login node, and a Machines page
// that remounts, or a retry loop, subscribes again and again. The host's own count
// of calls is the load, with every tool and file the sampler touches.
func TestSubscribingAgainAndAgainCostsTheHostOneSamplePerCadence(t *testing.T) {
	cadence := hostoperations.Topics[hostoperations.TopicMachineStats].Cadence
	for _, goos := range []string{"linux", "darwin"} {
		t.Run(goos, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				perSample := func() int64 {
					host := machinestatstest.Recorded(goos)
					sample(t, goos, host)
					return host.Calls.Load()
				}()
				host := machinestatstest.Recorded(goos)
				hub := subscription.New(clock.System{}, machinestats.Topic(goos, clock.System{}, host))
				const cycles, stay = 100, 1500 * time.Millisecond
				for range cycles {
					stream := subscriptiontest.Open(hub, 1, request)
					time.Sleep(stay)
					synctest.Wait()
					stream.Stop()
					synctest.Wait()
				}
				if allowed := perSample * (int64(cycles*stay/cadence) + 1); host.Calls.Load() > allowed {
					t.Fatalf("%d host calls in %s at a %s cadence, at most %d", host.Calls.Load(), cycles*stay, cadence, allowed)
				}
			})
		})
	}
}

func TestSystemHostFindsAndRunsATool(t *testing.T) {
	t.Setenv("PATH", t.TempDir())
	directory := t.TempDir()
	tool := filepath.Join(directory, "tool")
	if err := os.WriteFile(tool, []byte("#!/bin/sh\necho \"$1\"\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	host := machinestats.SystemHost{}
	if _, err := host.Find("tool"); !errors.Is(err, machinestats.ErrToolNotFound) {
		t.Fatalf("a tool off PATH with no candidate: %v", err)
	}
	found, err := host.Find("tool", filepath.Join(directory, "absent"), tool)
	if err != nil || found != tool {
		t.Fatalf("found %q, err %v", found, err)
	}
	output, err := host.Run(context.Background(), found, "hello")
	if err != nil || strings.TrimSpace(string(output)) != "hello" {
		t.Fatalf("output %q, err %v", output, err)
	}
	if _, err := host.ReadFile(tool); err != nil {
		t.Fatal(err)
	}
}
