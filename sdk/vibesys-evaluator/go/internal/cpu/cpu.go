// Package cpu measures loadgen's own CPU time and the CPUs it may use.
package cpu

import (
	"os"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// Used returns the CPU time (user plus system) this process has used so far.
func Used() time.Duration {
	var usage syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &usage); err != nil {
		return 0
	}
	return time.Duration(usage.Utime.Nano() + usage.Stime.Nano())
}

// Quota returns the CPUs this process may use: its cgroup's CPU quota (v2 cpu.max, or v1
// cfs_quota_us over cfs_period_us) or the machine's CPUs, and at most GOMAXPROCS.
func Quota() float64 {
	procs := float64(runtime.GOMAXPROCS(0))
	if quota, ok := cgroupV2("/sys/fs/cgroup/cpu.max"); ok {
		return min(quota, procs)
	}
	if quota, ok := cgroupV1("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "/sys/fs/cgroup/cpu/cpu.cfs_period_us"); ok {
		return min(quota, procs)
	}
	return procs
}

func cgroupV2(path string) (float64, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0, false
	}
	fields := strings.Fields(string(data))
	if len(fields) != 2 || fields[0] == "max" {
		return 0, false
	}
	return ratio(fields[0], fields[1])
}

func cgroupV1(quotaPath, periodPath string) (float64, bool) {
	quota, err := os.ReadFile(quotaPath)
	if err != nil {
		return 0, false
	}
	period, err := os.ReadFile(periodPath)
	if err != nil {
		return 0, false
	}
	return ratio(strings.TrimSpace(string(quota)), strings.TrimSpace(string(period)))
}

func ratio(quota, period string) (float64, bool) {
	q, err := strconv.ParseFloat(quota, 64)
	if err != nil || q <= 0 {
		return 0, false
	}
	p, err := strconv.ParseFloat(period, 64)
	if err != nil || p <= 0 {
		return 0, false
	}
	return min(q/p, float64(runtime.NumCPU())), true
}
