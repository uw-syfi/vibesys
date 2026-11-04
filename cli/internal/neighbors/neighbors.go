// Package neighbors finds the daemons of a machine: the processes whose first argument names
// them, of any user, as a proc file system lists them. It reads files only, so a test gives it a
// file system of its own.
package neighbors

import (
	"bufio"
	"bytes"
	"io/fs"
	"path"
	"slices"
	"strconv"
	"strings"
)

// Process is a process of the machine and the user it runs as, by number.
type Process struct {
	PID int
	UID int
}

// Find lists the processes of proc whose first argument is name or a path that ends in it, but
// self, in the order of their pids. A process that ends while it is read is left out, and so is one
// whose files cannot be read, such as another user's on a system that hides them.
func Find(proc fs.FS, name string, self int) []Process {
	entries, err := fs.ReadDir(proc, ".")
	if err != nil {
		return nil
	}
	var out []Process
	for _, e := range entries {
		pid, err := strconv.Atoi(e.Name())
		if err != nil || pid <= 0 || pid == self {
			continue
		}
		cmdline, err := fs.ReadFile(proc, e.Name()+"/cmdline")
		if err != nil {
			continue
		}
		first, _, _ := bytes.Cut(cmdline, []byte{0})
		if path.Base(string(first)) != name {
			continue
		}
		uid, ok := uidOf(proc, e.Name())
		if !ok {
			continue
		}
		out = append(out, Process{PID: pid, UID: uid})
	}
	slices.SortFunc(out, func(a, b Process) int { return a.PID - b.PID })
	return out
}

// uidOf reads the real user id of a process from its status file.
func uidOf(proc fs.FS, pid string) (int, bool) {
	status, err := fs.ReadFile(proc, pid+"/status")
	if err != nil {
		return 0, false
	}
	s := bufio.NewScanner(bytes.NewReader(status))
	for s.Scan() {
		rest, ok := strings.CutPrefix(s.Text(), "Uid:")
		if !ok {
			continue
		}
		fields := strings.Fields(rest)
		if len(fields) == 0 {
			return 0, false
		}
		uid, err := strconv.Atoi(fields[0])
		return uid, err == nil
	}
	return 0, false
}
