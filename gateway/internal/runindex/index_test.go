package runindex

import (
	"os"
	"path/filepath"
	"testing"
	"testing/quick"
)

func TestPublishedIndex(t *testing.T) {
	root, run := t.TempDir(), t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "runs"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(run, filepath.Join(root, "runs", "run-1")); err != nil {
		t.Fatal(err)
	}
	got, err := Resolve(root, "run-1")
	// Resolve macOS's /var alias in temp directories before comparing paths.
	resolved, resolveErr := filepath.EvalSymlinks(run)
	if err != nil || resolveErr != nil || got != filepath.Join(resolved, "run.log") {
		t.Fatalf("Resolve = %q, %v", got, err)
	}
	if err := os.Mkdir(filepath.Join(root, "runs", "directory"), 0700); err != nil {
		t.Fatal(err)
	}
	if _, err := Resolve(root, "directory"); err == nil {
		t.Fatal("accepted non-symlink entry")
	}
}

func TestRejectTraversal(t *testing.T) {
	root := t.TempDir()
	if err := quick.Check(func(s string) bool {
		_, err := Resolve(root, "../"+s)
		return err != nil
	}, nil); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"", ".", "..", "/absolute", "a/b", "a\\b", "a\x00b"} {
		if _, err := Resolve(root, id); err == nil {
			t.Fatalf("accepted %q", id)
		}
	}
}
