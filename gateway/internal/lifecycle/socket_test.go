package lifecycle

import (
	"errors"
	"os"
	"testing"
)

func TestSocketOwnership(t *testing.T) {
	// Unix socket paths are limited to 104 bytes on macOS, and its default temporary directory alone
	// comes close.
	root, err := os.MkdirTemp("/tmp", "gw-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	endpoint, err := Listen(root)
	if err != nil {
		t.Fatal(err)
	}
	defer endpoint.Close()
	info, err := os.Stat(SocketPath(root))
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("socket permissions = %v, %v", info, err)
	}
	if other, err := Listen(root); !errors.Is(err, ErrServing) {
		if other != nil {
			other.Close()
		}
		t.Fatalf("second listener = %v", err)
	}
	endpoint.Close()
	replacement, err := Listen(root)
	if err != nil {
		t.Fatal(err)
	}
	replacement.Close()
}
