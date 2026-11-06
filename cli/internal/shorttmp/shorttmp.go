// Package shorttmp keeps a test binary's temporary directories short enough for a Unix socket.
//
// A socket path is limited to about a hundred bytes, and the directories tests make (t.TempDir,
// os.MkdirTemp) live under TMPDIR, which the caller chooses: a long TMPDIR fails every test that
// binds a socket under it, for no reason in the test. A package whose tests bind sockets ends its
// TestMain with
//
//	os.Exit(shorttmp.Run(m))
//
// which moves TMPDIR to a short, local directory when the caller's is too long, so the result of
// the tests does not depend on the caller's environment.
package shorttmp

import (
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
)

// MaxLen is the longest temporary directory this package leaves alone. A test's directory adds
// its own name (t.TempDir appends the test's name, which can be sixty bytes) and a socket's
// name below that, and the whole must stay under the socket path limit.
const MaxLen = 16

// Candidates are the short directories offered in place of a long TMPDIR, tried in order. The
// first that Usable accepts is used.
func Candidates(getenv func(string) string) []string {
	out := []string{"/tmp", "/dev/shm", "/var/tmp"}
	if dir := getenv("XDG_RUNTIME_DIR"); dir != "" {
		out = append(out, dir)
	}
	return out
}

// Choose returns the temporary directory a test binary should use: current when it is short and usable
// (a network file system is not), and otherwise the first candidate that is short and usable. When
// there is none it returns current, and the tests that need a short path fail as they would have.
func Choose(current string, candidates []string, usable func(string) bool) string {
	if current != "" && len(current) <= MaxLen && usable(current) {
		return current
	}
	for _, c := range candidates {
		if len(c) <= MaxLen && usable(c) {
			return c
		}
	}
	return current
}

// Usable reports whether tests can keep sockets and programs in dir: it is a directory on local
// disk (the runtime directory refuses a network file system), a socket binds in it, and a
// program run from it executes (a tmpfs is sometimes mounted noexec).
func Usable(dir string) bool {
	if info, err := os.Stat(dir); err != nil || !info.IsDir() {
		return false
	}
	if network, err := runtimedir.System().IsNetwork(dir); err != nil || network {
		return false
	}
	probe, err := os.MkdirTemp(dir, "st")
	if err != nil {
		return false
	}
	defer os.RemoveAll(probe)
	l, err := net.Listen("unix", filepath.Join(probe, "s"))
	if err != nil {
		return false
	}
	l.Close()
	script := filepath.Join(probe, "x")
	if os.WriteFile(script, []byte("#!/bin/sh\nexit 0\n"), 0o700) != nil {
		return false
	}
	return exec.Command(script).Run() == nil
}

// Run points TMPDIR at a short directory when the caller's is long, runs the package's tests and
// returns their exit code. Child processes the tests start inherit the choice.
func Run(m *testing.M) int {
	current := os.Getenv("TMPDIR")
	if current == "" {
		current = os.TempDir()
	}
	if chosen := Choose(current, Candidates(os.Getenv), Usable); chosen != current {
		os.Setenv("TMPDIR", chosen)
	}
	return m.Run()
}
