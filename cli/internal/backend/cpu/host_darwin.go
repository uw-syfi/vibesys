//go:build darwin

package cpu

import (
	"context"
	"encoding/binary"
	"os"
	"runtime"

	"golang.org/x/sys/unix"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Host returns the System of the machine this process runs on.
func Host() System { return darwin{} }

// darwin reads the machine through sysctl. macOS gives a process no per-interval CPU time without
// the Mach interfaces, so the busy fraction is estimated from the load average: the one-minute
// load per logical CPU, held to 1. The load average counts runnable tasks, so a machine with more
// than one per CPU reads as fully busy.
type darwin struct{}

func (darwin) Machine(ctx context.Context) (core.MachineFacts, error) {
	if err := ctx.Err(); err != nil {
		return core.MachineFacts{}, err
	}
	host, err := os.Hostname()
	if err != nil {
		return core.MachineFacts{}, unavailable("the machine has no host name: %v", err)
	}
	model, err := unix.Sysctl("machdep.cpu.brand_string")
	if err != nil || model == "" {
		model = "unknown"
	}
	cpus, err := unix.SysctlUint32("hw.logicalcpu")
	if err != nil || cpus == 0 {
		return core.MachineFacts{}, unavailable("sysctl hw.logicalcpu: %v", err)
	}
	mem, err := unix.SysctlUint64("hw.memsize")
	if err != nil {
		return core.MachineFacts{}, unavailable("sysctl hw.memsize: %v", err)
	}
	return core.MachineFacts{
		HostID: host, OS: runtime.GOOS, Arch: runtime.GOARCH, CPUModel: model, LogicalCPUs: cpus, MemoryBytes: mem,
	}, nil
}

func (darwin) Read(ctx context.Context) (Reading, error) {
	if err := ctx.Err(); err != nil {
		return Reading{}, err
	}
	raw, err := unix.SysctlRaw("vm.loadavg")
	if err != nil || len(raw) < 4 {
		return Reading{}, unavailable("sysctl vm.loadavg: %v", err)
	}
	// struct loadavg { fixpt_t ldavg[3]; long fscale; }: the load is ldavg[0] / fscale.
	scale := float64(0)
	if len(raw) >= 24 {
		scale = float64(int64(binary.LittleEndian.Uint64(raw[16:24])))
	}
	if scale <= 0 {
		return Reading{}, unavailable("sysctl vm.loadavg: no scale")
	}
	load := float64(binary.LittleEndian.Uint32(raw[0:4])) / scale
	cpus, _ := unix.SysctlUint32("hw.logicalcpu")
	const resolution = 1 << 20
	busy := uint64(0)
	if cpus > 0 {
		busy = uint64(min(load/float64(cpus), 1) * resolution)
	}
	free, _ := unix.SysctlUint32("vm.page_free_count")
	page, _ := unix.SysctlUint32("hw.pagesize")
	return Reading{
		LoadAverage1m:        load,
		CPU:                  Counters{Busy: busy, Total: resolution},
		MemoryAvailableBytes: uint64(free) * uint64(page),
	}, nil
}
