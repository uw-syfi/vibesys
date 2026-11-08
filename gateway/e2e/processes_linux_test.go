//go:build e2e && linux

package e2e

import (
	"bytes"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// The leak checks of this tier read the live processes of the machine through /proc: the processes
// that carry a mark in their environment, and the path of each one's executable. macOS has no /proc,
// and a list read from nothing says "nothing leaked" about a machine it never looked at, so these
// are Linux's, and processes_other_test.go stops a test that reaches them anywhere else.

// processesMarked lists the live processes whose environment carries the mark, which every
// descendant of the observer inherits.
func processesMarked(mark string) []int {
	entries, _ := filepath.Glob("/proc/[0-9]*/environ")
	want := []byte("VIBESYS_E2E_MARK=" + mark + "\x00")
	var pids []int
	for _, entry := range entries {
		data, err := os.ReadFile(entry)
		if err != nil || !bytes.Contains(data, want) {
			continue
		}
		pid, err := strconv.Atoi(strings.Split(entry, "/")[2])
		if err == nil && running(pid) {
			pids = append(pids, pid)
		}
	}
	return pids
}

// processTable reads the live executable paths from proc.
func processTable() []proc {
	entries, _ := filepath.Glob("/proc/[0-9]*/exe")
	var table []proc
	for _, entry := range entries {
		target, err := os.Readlink(entry)
		if err != nil {
			continue
		}
		pid, err := strconv.Atoi(strings.Split(entry, "/")[2])
		if err == nil && running(pid) {
			table = append(table, proc{pid, target})
		}
	}
	return table
}

// processesOf lists the live processes running the executable at path.
func processesOf(path string) []int {
	var pids []int
	for _, p := range processTable() {
		if p.exe == path {
			pids = append(pids, p.pid)
		}
	}
	return pids
}
