package lifecycle

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestSocketOwnership(t *testing.T) {
	// Unix socket paths are limited to 104 bytes on macOS; its default temporary
	// directory alone can exceed that once the API hash is appended.
	root, err := os.MkdirTemp("/tmp", "gw-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	endpoint, err := Listen(root, "hash")
	if err != nil {
		t.Fatal(err)
	}
	defer endpoint.Close()
	for path, want := range map[string]os.FileMode{SocketPath(root, "hash"): 0600, filepath.Join(root, "gateway"): 0700, filepath.Join(root, "gateway", "hash"): 0700} {
		info, err := os.Stat(path)
		if err != nil || info.Mode().Perm() != want {
			t.Fatalf("permissions %s = %v, %v", path, info, err)
		}
	}
	if other, err := Listen(root, "hash"); !errors.Is(err, ErrServing) {
		if other != nil {
			other.Close()
		}
		t.Fatalf("second listener = %v", err)
	}
	independent, err := Listen(root, "other-hash")
	if err != nil {
		t.Fatal(err)
	}
	independent.Close()
	endpoint.Close()
	replacement, err := Listen(root, "hash")
	if err != nil {
		t.Fatal(err)
	}
	replacement.Close()
}
