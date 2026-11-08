//go:build linux

package proctable

import (
	"bytes"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"strconv"
	"strings"
	"syscall"
)

const procRoot = "/proc"

// table lists /proc, and for each process of the caller's user that runs reads its stat file, for
// its parent and its state, and its environ file. A process that ended since the listing has no file
// (ENOENT, or ESRCH from a read that began before it ended). One whose environ this user may not
// read (EACCES) is one that cleared its dumpable flag, as an ssh-agent does, which no test starts;
// it is kept without an environment, for its children. A zombie has ended and is left out. Anything
// else is a /proc that cannot be read.
func table() ([]process, error) {
	entries, err := os.ReadDir(procRoot)
	if err != nil {
		return nil, fmt.Errorf("proctable: listing %s: %w", procRoot, err)
	}
	uid := uint32(os.Geteuid())
	var procs []process
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil || pid <= 0 {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			if gone(err) {
				continue
			}
			return nil, fmt.Errorf("proctable: reading %s/%d: %w", procRoot, pid, err)
		}
		if owner, ok := info.Sys().(*syscall.Stat_t); !ok || owner.Uid != uid {
			continue
		}
		state, ppid, err := readStat(pid)
		if err != nil {
			if gone(err) {
				continue
			}
			return nil, err
		}
		if state == 'Z' || state == 'X' {
			continue
		}
		data, err := os.ReadFile(procRoot + "/" + entry.Name() + "/environ")
		var environ []string
		switch {
		case err == nil && len(data) > 0:
			environ = strings.Split(strings.TrimSuffix(string(data), "\x00"), "\x00")
		case err == nil, gone(err), errors.Is(err, fs.ErrPermission):
		default:
			return nil, fmt.Errorf("proctable: reading the environment of process %d: %w", pid, err)
		}
		procs = append(procs, process{pid: pid, ppid: ppid, environ: environ})
	}
	return procs, nil
}

// running reads the state letter of /proc/<pid>/stat: Z is a zombie and X a process being removed.
func running(pid int) (bool, error) {
	state, _, err := readStat(pid)
	if gone(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return state != 'Z' && state != 'X', nil
}

// gone says that an error is the file system's way of saying a process is not there.
func gone(err error) bool {
	return errors.Is(err, fs.ErrNotExist) || errors.Is(err, syscall.ESRCH)
}

// readStat returns the state letter and the parent of a process.
func readStat(pid int) (state byte, ppid int, err error) {
	stat, err := os.ReadFile(procRoot + "/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		if gone(err) {
			return 0, 0, err
		}
		return 0, 0, fmt.Errorf("proctable: reading the state of process %d: %w", pid, err)
	}
	// The state and the parent follow the command name, which is in parentheses and may hold
	// anything, even a parenthesis, so they are read after the last one.
	end := bytes.LastIndexByte(stat, ')')
	if end < 0 {
		return 0, 0, fmt.Errorf("proctable: %s/%d/stat is not in the form of a stat file: %q", procRoot, pid, stat)
	}
	fields := strings.Fields(string(stat[end+1:]))
	if len(fields) < 2 || len(fields[0]) != 1 {
		return 0, 0, fmt.Errorf("proctable: %s/%d/stat is not in the form of a stat file: %q", procRoot, pid, stat)
	}
	ppid, err = strconv.Atoi(fields[1])
	if err != nil {
		return 0, 0, fmt.Errorf("proctable: %s/%d/stat does not say a parent: %q", procRoot, pid, stat)
	}
	return fields[0][0], ppid, nil
}
