package machinestats

import (
	"bytes"
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/clock"
)

const (
	procStat    = "/proc/stat"
	procMeminfo = "/proc/meminfo"

	// procStatFields counts the columns that partition CPU time: user, nice,
	// system, idle, iowait, irq, softirq, steal. Guest time is inside user.
	procStatFields = 8
	idleField      = 3
	iowaitField    = 4
	// cpuWindow is the first sample's window, before earlier counters exist.
	cpuWindow = time.Second
)

// linux reads procfs. Utilization is a delta of cumulative counters, so it keeps
// the last read.
type linux struct {
	ticks    clock.Clock
	previous *[procStatFields]uint64
}

func (l *linux) cpu(ctx context.Context, host Host) (*apiv1.CpuStats, error) {
	if l.previous == nil {
		first, err := readProcStat(host)
		if err != nil {
			return nil, err
		}
		l.previous = &first
		ticker := l.ticks.NewTicker(cpuWindow)
		defer ticker.Stop()
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-ticker.Ticks():
		}
	}
	current, err := readProcStat(host)
	if err != nil {
		return nil, err
	}
	stats, err := utilization(*l.previous, current)
	if err == nil {
		l.previous = &current
	}
	return stats, err
}

func readProcStat(host Host) ([procStatFields]uint64, error) {
	data, err := host.ReadFile(procStat)
	if err != nil {
		return [procStatFields]uint64{}, err
	}
	return parseProcStat(data)
}

func parseProcStat(data []byte) ([procStatFields]uint64, error) {
	var counters [procStatFields]uint64
	line, _, _ := bytes.Cut(data, []byte("\n"))
	fields := strings.Fields(string(line))
	if len(fields) <= procStatFields || fields[0] != "cpu" {
		return counters, parseFailure("first line of %s", procStat)
	}
	for index := range counters {
		value, err := strconv.ParseUint(fields[index+1], 10, 64)
		if err != nil {
			return counters, parseFailure("%s field %d", procStat, index)
		}
		counters[index] = value
	}
	return counters, nil
}

// utilization clamps each field's delta at zero, because the kernel documents
// that iowait can go backwards. No elapsed ticks means no reading, not 0%.
func utilization(previous, current [procStatFields]uint64) (*apiv1.CpuStats, error) {
	var total, idle uint64
	for index := range current {
		if current[index] <= previous[index] {
			continue
		}
		delta := current[index] - previous[index]
		total += delta
		if index == idleField || index == iowaitField {
			idle += delta
		}
	}
	if total == 0 || idle > total {
		return nil, fmt.Errorf("%s: no CPU time elapsed", procStat)
	}
	return &apiv1.CpuStats{UtilizationPercent: 100 * float64(total-idle) / float64(total)}, nil
}

func (*linux) memory(_ context.Context, host Host) (*apiv1.MemoryStats, error) {
	data, err := host.ReadFile(procMeminfo)
	if err != nil {
		return nil, err
	}
	return parseMeminfo(data)
}

func parseMeminfo(data []byte) (*apiv1.MemoryStats, error) {
	total, totalFound, available, availableFound := uint64(0), false, uint64(0), false
	for line := range strings.Lines(string(data)) {
		name, rest, found := strings.Cut(line, ":")
		if !found || (name != "MemTotal" && name != "MemAvailable") {
			continue
		}
		fields := strings.Fields(rest)
		if len(fields) != 2 || fields[1] != "kB" {
			return nil, parseFailure("%s line %s", procMeminfo, name)
		}
		value, err := strconv.ParseUint(fields[0], 10, 54)
		if err != nil {
			return nil, parseFailure("%s line %s", procMeminfo, name)
		}
		if name == "MemTotal" {
			total, totalFound = value*1024, true
		} else {
			available, availableFound = value*1024, true
		}
	}
	if !totalFound || !availableFound {
		return nil, parseFailure("%s has no MemTotal or MemAvailable", procMeminfo)
	}
	return &apiv1.MemoryStats{UsedBytes: total - min(available, total), TotalBytes: total}, nil
}

func (*linux) gpus(ctx context.Context, host Host) (*apiv1.GpuList, error) {
	return smiGpus(ctx, host)
}
