//go:build linux

package main

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func TestRealDaemonProcessName(t *testing.T) {
	t.Parallel()
	m := newMachine(t)
	status := func() int {
		t.Helper()
		r := m.cli("daemon", "status")
		var pid int
		if r.code != exitOK {
			t.Fatalf("status: %+v", r)
		}
		if _, err := fmt.Sscanf(r.stdout, "running\tpid %d", &pid); err != nil || pid == 0 {
			t.Fatalf("status: %+v: %v", r, err)
		}
		return pid
	}
	ensure := func() {
		t.Helper()
		if r := m.cli("daemon", "ensure"); r.code != exitOK {
			t.Fatalf("ensure: %+v", r)
		}
	}
	ensure()
	first := status()
	data, err := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", first))
	if err != nil {
		t.Fatal(err)
	}
	if got := bytes.Split(data, []byte{0})[0]; string(got) != "vibesysd" {
		t.Fatalf("argv[0] = %q", got)
	}
	exe, err := os.Readlink(fmt.Sprintf("/proc/%d/exe", first))
	if err != nil {
		t.Fatal(err)
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	self, err = filepath.EvalSymlinks(self)
	if err != nil {
		t.Fatal(err)
	}
	if exe != self {
		t.Fatalf("exe = %q, want %q", exe, self)
	}
	ensure()
	if got := status(); got != first {
		t.Fatalf("second client got %d, want %d", got, first)
	}
	if r := m.cli("daemon", "stop"); r.code != exitOK {
		t.Fatalf("stop: %+v", r)
	}
	ensure()
	second := status()
	if second == first {
		t.Fatalf("daemon restarted with same pid %d", second)
	}
	ensure()
	if got := status(); got != second {
		t.Fatalf("second client got %d, want %d", got, second)
	}
}
