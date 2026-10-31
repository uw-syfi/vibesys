package machinestats_test

import (
	"context"
	"encoding/json"
	"errors"
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
	"github.com/uw-syfi/vibesys/gateway/internal/machinestats"
	"github.com/uw-syfi/vibesys/gateway/internal/machinestats/machinestatstest"
)

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
