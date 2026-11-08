// Package proctable reads the machine's process table for the tests that run real processes: which
// processes belong to a directory the test made, and whether one still runs. Test support only: a
// test that starts a daemon uses it to end everything the daemon started, and to say the end
// happened.
//
// A process belongs to a directory when it carries a variable set to a path under it, which every
// process the tool starts inherits, or when it descends from one that does. The descent is what
// finds a program the system hides the environment of: macOS shows another process's environment
// only when the program is not one it protects, so the daemon and the supervisors, which are the
// tool, show it, and the sh or sleep a job runs does not. Such a program is found as the child of the
// supervisor that ran it.
//
// The table is read the way each system gives it. Linux lists /proc and reads a process's environ
// and stat files. macOS has no /proc, so it asks the kernel with sysctl: kern.proc.all and
// kern.proc.pid for the list and the state, and kern.procargs2 for the arguments and environment.
// A system with neither fails with ErrUnsupported on every call.
//
// Only a definite answer means a process is not there. A process that ended after it was listed, or
// that is a zombie, is not running. A process whose environment the system refuses this user is
// another user's, and is left out. Every other failure to read the table is returned as an error,
// never as an empty result: a reader that took "cannot read" for "nothing to find" passes on a
// system where the table cannot be read, and leaves the processes it was to end running. The
// archcheck rule that a file naming /proc is Linux-only holds the callers to the same.
package proctable

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
)

// ErrUnsupported is returned, wrapped, by every call on a system whose process table this package
// cannot read.
var ErrUnsupported = errors.New("proctable: this system's process table cannot be read")

// WithEnvUnder returns the pids, in order, of the processes of the caller's user, other than the
// caller, that run and either have name set to dir or to a path under dir in their environment, or
// descend from one that does. A path that merely starts with dir's text, such as "/a/b2" for dir
// "/a/b", is not under it. The caller's own process is neither listed nor passed through.
//
// It returns an error when the table cannot be read, and then no pids: an empty list means no
// process is there.
func WithEnvUnder(name, dir string) ([]int, error) {
	if name == "" || strings.ContainsAny(name, "=\x00") {
		return nil, fmt.Errorf("proctable: %q is not the name of an environment variable", name)
	}
	if dir == "" {
		return nil, errors.New("proctable: an empty directory holds every path")
	}
	procs, err := table()
	if err != nil {
		return nil, err
	}
	return marked(procs, os.Getpid(), func(environ []string) bool {
		value, ok := lookup(environ, name)
		return ok && under(dir, value)
	}), nil
}

// Running says whether the process exists and has not ended. A process that ended and waits for its
// parent to collect it, a zombie, has ended; one that is stopped has not. It returns an error when
// the table cannot be read, and not false.
func Running(pid int) (bool, error) {
	if pid <= 0 {
		return false, fmt.Errorf("proctable: %d is not a process id", pid)
	}
	return running(pid)
}

// process is a live process of the caller's user, as the table shows it.
type process struct {
	pid, ppid int
	// environ is the NAME=value entries of its environment, or nil when the system does not show
	// them: the program is one the system protects, or another user's.
	environ []string
}

// marked returns, in order, the pids of the processes of the table that accept takes the
// environment of, and of those that descend from one. self is neither returned nor passed through.
func marked(procs []process, self int, accept func(environ []string) bool) []int {
	children := map[int][]int{}
	for _, p := range procs {
		children[p.ppid] = append(children[p.ppid], p.pid)
	}
	found := map[int]bool{}
	var queue []int
	for _, p := range procs {
		if p.pid != self && accept(p.environ) {
			found[p.pid] = true
			queue = append(queue, p.pid)
		}
	}
	for len(queue) > 0 {
		pid := queue[0]
		queue = queue[1:]
		for _, child := range children[pid] {
			if child != self && !found[child] {
				found[child] = true
				queue = append(queue, child)
			}
		}
	}
	pids := make([]int, 0, len(found))
	for pid := range found {
		pids = append(pids, pid)
	}
	slices.Sort(pids)
	return pids
}

// lookup returns the value of name in environ, a list of NAME=value entries, and whether it is set.
// When it is set more than once the first counts, as it does for the C library's getenv.
func lookup(environ []string, name string) (string, bool) {
	for _, entry := range environ {
		if key, value, ok := strings.Cut(entry, "="); ok && key == name {
			return value, true
		}
	}
	return "", false
}

// under says whether path is dir or a path inside it, once both are cleaned, so that "/a/b/../c" is
// not inside "/a/b".
func under(dir, path string) bool {
	dir, path = filepath.Clean(dir), filepath.Clean(path)
	return path == dir || strings.HasPrefix(path, strings.TrimSuffix(dir, "/")+"/")
}

// ParseProcArgs2 reads what the macOS kernel returns for sysctl kern.procargs2 of a process, and
// returns its arguments and its environment as NAME=value entries. The layout is the argument count
// as a native-endian 32-bit integer, the path of the executable and the NUL bytes that pad it, the
// arguments, then the environment, each string ended by a NUL. The environment ends at the first
// empty string; what follows it, such as the "executable_path=" strings the kernel adds, is not the
// process's environment. For a program the system protects the layout ends after the arguments, and
// the environment is empty.
//
// An empty first argument cannot be told from the padding before it and is not supported. The
// function is exported so that a test on any system can feed it the layout; only macOS produces it.
func ParseProcArgs2(raw []byte) (args, environ []string, err error) {
	if len(raw) < 4 {
		return nil, nil, fmt.Errorf("proctable: kern.procargs2 returned %d bytes, fewer than the 4 of its argument count", len(raw))
	}
	count := int(int32(binary.NativeEndian.Uint32(raw)))
	if count < 0 {
		return nil, nil, fmt.Errorf("proctable: kern.procargs2 says %d arguments", count)
	}
	rest := raw[4:]
	// The executable's path, then the zero bytes that align what follows it.
	_, rest, found := bytes.Cut(rest, []byte{0})
	if !found {
		return nil, nil, errors.New("proctable: kern.procargs2 ends inside the executable path")
	}
	rest = bytes.TrimLeft(rest, "\x00")
	for len(args) < count {
		arg, after, found := bytes.Cut(rest, []byte{0})
		if !found {
			return nil, nil, fmt.Errorf("proctable: kern.procargs2 holds %d of its %d arguments", len(args), count)
		}
		args, rest = append(args, string(arg)), after
	}
	for len(rest) > 0 {
		entry, after, _ := bytes.Cut(rest, []byte{0})
		if len(entry) == 0 {
			break
		}
		environ, rest = append(environ, string(entry)), after
	}
	return args, environ, nil
}
