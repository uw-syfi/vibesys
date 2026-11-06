package k8s

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func executable(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
}

func whichLoadgen(t *testing.T, path string) string {
	t.Helper()
	cmd := exec.Command("sh", "-c", pathFixup+"command -v loadgen")
	cmd.Env = []string{"PATH=" + path}
	out, _ := cmd.Output()
	return strings.TrimSpace(string(out))
}

// A task image that installs the wheel alone has loadgen only beside vibesys-evaluator, not on
// PATH: the probe pod still finds it. What is on PATH wins.
func TestPathFixupFindsLoadgenBesideTheCore(t *testing.T) {
	root := t.TempDir()
	executable(t, filepath.Join(root, "wheel", "vibesys-evaluator"))
	executable(t, filepath.Join(root, "wheel", "loadgen"))
	if err := os.MkdirAll(filepath.Join(root, "bin"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(root, "wheel", "vibesys-evaluator"), filepath.Join(root, "bin", "vibesys-evaluator")); err != nil {
		t.Fatal(err)
	}
	system := "/usr/bin:/bin"
	if got, want := whichLoadgen(t, filepath.Join(root, "bin")+":"+system), filepath.Join(root, "wheel", "loadgen"); got != want {
		t.Fatalf("loadgen at %q, want %q", got, want)
	}
	executable(t, filepath.Join(root, "bin", "loadgen"))
	if got, want := whichLoadgen(t, filepath.Join(root, "bin")+":"+system), filepath.Join(root, "bin", "loadgen"); got != want {
		t.Fatalf("loadgen at %q, want the one on PATH %q", got, want)
	}
	if got := whichLoadgen(t, system); got != "" {
		t.Fatalf("found %q with no install", got)
	}
}
