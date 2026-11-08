//go:build darwin

package proctable

import (
	"errors"
	"fmt"
	"os"

	"golang.org/x/sys/unix"
)

// The process states of kinfo_proc's p_stat, from <sys/proc.h>.
const (
	stateIdle   = 1 // being created
	stateZombie = 5 // ended, waiting for its parent to collect it
)

// table asks the kernel for every process (kern.proc.all) and, for those of this user that run,
// reads the environment (kern.procargs2). The kernel answers EINVAL for a process of another user,
// for one that is not there and for a zombie, so only the caller's own live processes are asked,
// and an EINVAL (or ESRCH) for one of those means it ended, or is being replaced by an exec, since
// the listing. A caller that must end every process re-lists after it signals, as the sweep of a
// test machine does, so a process skipped in the middle of its exec is found by the next listing.
// A program the system protects, such as sh, answers with its arguments and no environment; it is
// kept without one, for its children. The idle processes, which are being created, are left out:
// they have no arguments yet.
func table() ([]process, error) {
	all, err := unix.SysctlKinfoProcSlice("kern.proc.all")
	if err != nil {
		return nil, fmt.Errorf("proctable: listing the processes with sysctl kern.proc.all: %w", err)
	}
	uid := uint32(os.Geteuid())
	var procs []process
	for _, p := range all {
		pid := int(p.Proc.P_pid)
		if pid <= 0 || p.Eproc.Ucred.Uid != uid || p.Proc.P_stat == stateZombie || p.Proc.P_stat == stateIdle {
			continue
		}
		raw, err := unix.SysctlRaw("kern.procargs2", pid)
		if errors.Is(err, unix.EINVAL) || errors.Is(err, unix.ESRCH) {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("proctable: reading the arguments of process %d with sysctl kern.procargs2: %w", pid, err)
		}
		_, environ, err := ParseProcArgs2(raw)
		if err != nil {
			return nil, fmt.Errorf("process %d: %w", pid, err)
		}
		procs = append(procs, process{pid: pid, ppid: int(p.Eproc.Ppid), environ: environ})
	}
	return procs, nil
}

// running asks the kernel for the process alone (kern.proc.pid), which returns no entry for a pid
// that is not there.
func running(pid int) (bool, error) {
	found, err := unix.SysctlKinfoProcSlice("kern.proc.pid", pid)
	if err != nil {
		return false, fmt.Errorf("proctable: reading the state of process %d with sysctl kern.proc.pid: %w", pid, err)
	}
	for _, p := range found {
		if int(p.Proc.P_pid) == pid {
			return p.Proc.P_stat != stateZombie, nil
		}
	}
	return false, nil
}
