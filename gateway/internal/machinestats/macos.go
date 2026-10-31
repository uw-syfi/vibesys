package machinestats

import (
	"context"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
)

// macos reads the system tools, which need no privileges. Apple GPUs use unified
// memory, so a GPU's memory total is the machine's.
type macos struct{}

var (
	topIdle    = regexp.MustCompile(`^CPU usage: .*, ([0-9.]+)% idle\s*$`)
	vmPageSize = regexp.MustCompile(`page size of ([0-9]+) bytes`)
	vmPages    = regexp.MustCompile(`(?m)^(Pages active|Pages wired down|Pages occupied by compressor):\s+([0-9]+)\.$`)
	iorModel   = regexp.MustCompile(`(?m)^\s*"model" = "([^"\n]*)"\s*$`)
	iorBusy    = regexp.MustCompile(`"Device Utilization %"=([0-9]+)`)
	iorMemory  = regexp.MustCompile(`"In use system memory"=([0-9]+)`)
)

// vmPageLines counts the vm_stat lines parseVMStat needs.
const vmPageLines = 3

func tool(ctx context.Context, host Host, name, path string, args ...string) ([]byte, error) {
	found, err := host.Find(name, path)
	if err != nil {
		return nil, err
	}
	return host.Run(ctx, found, args...)
}

func (macos) cpu(ctx context.Context, host Host) (*apiv1.CpuStats, error) {
	// top's first report covers all time since boot, so only its last counts.
	output, err := tool(ctx, host, "top", "/usr/bin/top", "-l", "2", "-n", "0", "-s", "1")
	if err != nil {
		return nil, err
	}
	return parseTop(output)
}

func parseTop(output []byte) (*apiv1.CpuStats, error) {
	var last string
	for line := range strings.Lines(string(output)) {
		if strings.HasPrefix(line, "CPU usage:") {
			last = line
		}
	}
	match := topIdle.FindStringSubmatch(last)
	if match == nil {
		return nil, parseFailure("top has no CPU usage line")
	}
	idle, err := strconv.ParseFloat(match[1], 64)
	if err != nil || idle < 0 || idle > 100 {
		return nil, parseFailure("top idle percentage")
	}
	return &apiv1.CpuStats{UtilizationPercent: 100 - idle}, nil
}

func physicalMemory(ctx context.Context, host Host) (uint64, error) {
	output, err := tool(ctx, host, "sysctl", "/usr/sbin/sysctl", "-n", "hw.memsize")
	if err != nil {
		return 0, err
	}
	total, err := strconv.ParseUint(strings.TrimSpace(string(output)), 10, 63)
	if err != nil || total == 0 {
		return 0, parseFailure("sysctl hw.memsize")
	}
	return total, nil
}

func (macos) memory(ctx context.Context, host Host) (*apiv1.MemoryStats, error) {
	total, err := physicalMemory(ctx, host)
	if err != nil {
		return nil, err
	}
	output, err := tool(ctx, host, "vm_stat", "/usr/bin/vm_stat")
	if err != nil {
		return nil, err
	}
	used, err := parseVMStat(output)
	if err != nil {
		return nil, err
	}
	return &apiv1.MemoryStats{UsedBytes: min(used, total), TotalBytes: total}, nil
}

// parseVMStat counts what Activity Monitor calls memory used: active, wired and
// compressed pages. Inactive pages are reclaimable.
func parseVMStat(output []byte) (uint64, error) {
	size := vmPageSize.FindSubmatch(output)
	if size == nil {
		return 0, parseFailure("vm_stat has no page size")
	}
	page, err := strconv.ParseUint(string(size[1]), 10, 24)
	if err != nil {
		return 0, parseFailure("vm_stat page size")
	}
	matches := vmPages.FindAllSubmatch(output, -1)
	if len(matches) != vmPageLines {
		return 0, parseFailure("vm_stat has %d of its page lines", len(matches))
	}
	var used uint64
	for _, match := range matches {
		pages, err := strconv.ParseUint(string(match[2]), 10, 32)
		if err != nil {
			return 0, parseFailure("vm_stat page count")
		}
		used += pages * page
	}
	return used, nil
}

func (macos) gpus(ctx context.Context, host Host) (*apiv1.GpuList, error) {
	total, err := physicalMemory(ctx, host)
	if err != nil {
		return nil, err
	}
	output, err := tool(ctx, host, "ioreg", "/usr/sbin/ioreg", "-r", "-d", "1", "-c", "IOAccelerator")
	if err != nil {
		return nil, err
	}
	return parseIORegistry(output, total)
}

// parseIORegistry reads one accelerator per top-level block. A block without its
// statistics refuses the section, as a bad nvidia-smi row does.
func parseIORegistry(output []byte, total uint64) (*apiv1.GpuList, error) {
	gpus := &apiv1.GpuList{}
	for block := range strings.SplitSeq("\n"+string(output), "\n+-o ") {
		if strings.TrimSpace(block) == "" {
			continue
		}
		model := iorModel.FindStringSubmatch(block)
		busy := iorBusy.FindStringSubmatch(block)
		used := iorMemory.FindStringSubmatch(block)
		if model == nil || busy == nil || used == nil {
			return nil, parseFailure("ioreg accelerator lacks a statistic")
		}
		percent, busyErr := strconv.ParseUint(busy[1], 10, 8)
		bytes, usedErr := strconv.ParseUint(used[1], 10, 64)
		name := model[1]
		switch {
		case busyErr != nil, usedErr != nil, percent > 100:
			return nil, parseFailure("ioreg statistic is out of range")
		case name == "", len(name) > maxGpuNameSize, !utf8.ValidString(name):
			return nil, parseFailure("ioreg model")
		case len(gpus.Gpus) == maxGpus:
			return nil, parseFailure("ioreg lists over %d GPUs", maxGpus)
		}
		gpus.Gpus = append(gpus.Gpus, &apiv1.GpuStats{
			Index: uint32(len(gpus.Gpus)), Name: name, UtilizationPercent: float64(percent),
			MemoryUsedBytes: min(bytes, total), MemoryTotalBytes: total,
		})
	}
	return gpus, nil
}
