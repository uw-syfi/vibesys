package cpu

import (
	"bufio"
	"context"
	"fmt"
	"io/fs"
	"math"
	"os"
	"runtime"
	"strconv"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// ParseCPUInfo reads /proc/cpuinfo: the model name of the first processor that has one, and how
// many processors there are. The model name is empty on a machine whose cpuinfo does not carry one.
func ParseCPUInfo(text string) (model string, processors int) {
	scanner := bufio.NewScanner(strings.NewReader(text))
	for scanner.Scan() {
		key, value, ok := strings.Cut(scanner.Text(), ":")
		if !ok {
			continue
		}
		switch strings.TrimSpace(key) {
		case "processor":
			processors++
		case "model name", "Model", "Hardware", "cpu model", "Processor":
			if model == "" {
				model = strings.Join(strings.Fields(value), " ")
			}
		}
	}
	return model, processors
}

// ParseMemInfo reads /proc/meminfo: the total memory and the memory available to start a program,
// in bytes. MemAvailable is the kernel's own estimate; a kernel that does not report it gets the
// free memory instead, which understates it. ok is false when the total is missing.
func ParseMemInfo(text string) (total, available uint64, ok bool) {
	var free uint64
	var haveAvailable bool
	scanner := bufio.NewScanner(strings.NewReader(text))
	for scanner.Scan() {
		key, value, found := strings.Cut(scanner.Text(), ":")
		if !found {
			continue
		}
		bytes, parsed := kibibytes(value)
		if !parsed {
			continue
		}
		switch key {
		case "MemTotal":
			total, ok = bytes, true
		case "MemAvailable":
			available, haveAvailable = bytes, true
		case "MemFree":
			free = bytes
		}
	}
	if !haveAvailable {
		available = free
	}
	return total, min(available, total), ok
}

// kibibytes reads the "123 kB" of a meminfo line as a number of bytes.
func kibibytes(value string) (uint64, bool) {
	fields := strings.Fields(value)
	if len(fields) == 0 {
		return 0, false
	}
	n, err := strconv.ParseUint(fields[0], 10, 64)
	if err != nil || n > math.MaxUint64/1024 {
		return 0, false
	}
	if len(fields) > 1 && strings.EqualFold(fields[1], "kB") {
		return n * 1024, true
	}
	return n, true
}

// ParseLoadAvg reads /proc/loadavg: the one-minute load average.
func ParseLoadAvg(text string) (float64, error) {
	fields := strings.Fields(text)
	if len(fields) == 0 {
		return 0, fmt.Errorf("loadavg: empty")
	}
	load, err := strconv.ParseFloat(fields[0], 64)
	if err != nil || math.IsNaN(load) || math.IsInf(load, 0) || load < 0 {
		return 0, fmt.Errorf("loadavg: %q is not a load average", fields[0])
	}
	return load, nil
}

// ParseStat reads the aggregate "cpu" line of /proc/stat. Busy is the time spent in user, nice,
// system, interrupt, soft interrupt and steal modes, and idle is idle plus waiting for I/O. Guest
// time is already counted in user time.
func ParseStat(text string) (Counters, error) {
	scanner := bufio.NewScanner(strings.NewReader(text))
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) == 0 || fields[0] != "cpu" {
			continue
		}
		if len(fields) < 5 {
			return Counters{}, fmt.Errorf("stat: the cpu line has %d fields, want at least 4 times", len(fields)-1)
		}
		var t [8]uint64 // user nice system idle iowait irq softirq steal
		for i := 0; i < len(t) && i+1 < len(fields); i++ {
			n, err := strconv.ParseUint(fields[i+1], 10, 64)
			if err != nil {
				return Counters{}, fmt.Errorf("stat: %q is not a time", fields[i+1])
			}
			t[i] = n
		}
		busy := t[0] + t[1] + t[2] + t[5] + t[6] + t[7]
		return Counters{Busy: busy, Total: busy + t[3] + t[4]}, nil
	}
	return Counters{}, fmt.Errorf("stat: no cpu line")
}

// Proc is a System over a /proc file tree, as Linux has. FS is rooted at /proc.
type Proc struct {
	FS fs.FS
	// Hostname names the machine.
	Hostname func() (string, error)
	OS, Arch string
	// Processors is the fallback logical CPU count for a cpuinfo that lists none.
	Processors int
}

var _ System = (*Proc)(nil)

// Machine implements System.
func (p *Proc) Machine(ctx context.Context) (core.MachineFacts, error) {
	if err := ctx.Err(); err != nil {
		return core.MachineFacts{}, err
	}
	host, err := p.Hostname()
	if err != nil || host == "" {
		return core.MachineFacts{}, unavailable("the machine has no host name: %v", err)
	}
	cpuinfo, err := fs.ReadFile(p.FS, "cpuinfo")
	if err != nil {
		return core.MachineFacts{}, unavailable("reading /proc/cpuinfo: %v", err)
	}
	meminfo, err := fs.ReadFile(p.FS, "meminfo")
	if err != nil {
		return core.MachineFacts{}, unavailable("reading /proc/meminfo: %v", err)
	}
	model, processors := ParseCPUInfo(string(cpuinfo))
	if processors == 0 {
		processors = p.Processors
	}
	if model == "" {
		model = "unknown"
	}
	total, _, ok := ParseMemInfo(string(meminfo))
	if !ok || processors <= 0 {
		return core.MachineFacts{}, unavailable("/proc does not say how many CPUs or how much memory the machine has")
	}
	return core.MachineFacts{
		HostID: host, OS: p.OS, Arch: p.Arch, CPUModel: model,
		LogicalCPUs: uint32(processors), MemoryBytes: total,
	}, nil
}

// Read implements System.
func (p *Proc) Read(ctx context.Context) (Reading, error) {
	if err := ctx.Err(); err != nil {
		return Reading{}, err
	}
	var r Reading
	loadText, err := fs.ReadFile(p.FS, "loadavg")
	if err != nil {
		return r, unavailable("reading /proc/loadavg: %v", err)
	}
	if r.LoadAverage1m, err = ParseLoadAvg(string(loadText)); err != nil {
		return r, unavailable("%v", err)
	}
	statText, err := fs.ReadFile(p.FS, "stat")
	if err != nil {
		return r, unavailable("reading /proc/stat: %v", err)
	}
	if r.CPU, err = ParseStat(string(statText)); err != nil {
		return r, unavailable("%v", err)
	}
	memText, err := fs.ReadFile(p.FS, "meminfo")
	if err != nil {
		return r, unavailable("reading /proc/meminfo: %v", err)
	}
	_, r.MemoryAvailableBytes, _ = ParseMemInfo(string(memText))
	return r, nil
}

// procHost is the Proc of a Linux machine.
func procHost() *Proc {
	return &Proc{FS: os.DirFS("/proc"), Hostname: os.Hostname, OS: runtime.GOOS, Arch: runtime.GOARCH, Processors: runtime.NumCPU()}
}
