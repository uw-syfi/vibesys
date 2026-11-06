package lifecycle_test

import (
	"encoding/hex"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"testing/quick"
	"time"

	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
)

// A root's socket is a function of the root alone, inside it whenever it fits in a socket address on
// both systems, and otherwise a short name of the root. Two roots never share one.
func TestTheSocketOfARootIsOneShortPathDerivedFromTheRootAlone(t *testing.T) {
	seen := map[string]string{}
	if err := quick.Check(func(name []byte, depth uint8) bool {
		root := "/" + hex.EncodeToString(name)
		for range int(depth) % 8 {
			root += "/" + strings.Repeat("d", 19)
		}
		socket := lifecycle.SocketPath(root)
		if socket != lifecycle.SocketPath(root) || len(socket) > lifecycle.MaxSocketPath {
			t.Logf("root %q: socket %q", root, socket)
			return false
		}
		if inside := filepath.Join(root, "host.sock"); len(inside) <= lifecycle.MaxSocketPath && socket != inside {
			t.Logf("root %q fits, and got %q", root, socket)
			return false
		}
		if other, ok := seen[socket]; ok && other != root {
			t.Logf("roots %q and %q share %q", other, root, socket)
			return false
		}
		seen[socket] = root
		return true
	}, nil); err != nil {
		t.Fatal(err)
	}
}

// A root too long for a socket address gets a host all the same, and whoever computes its socket
// reaches that host.
func TestARootTooLongForASocketAddressStillServes(t *testing.T) {
	root := filepath.Join(t.TempDir(), strings.Repeat("r", 120))
	socket := lifecycle.SocketPath(root)
	if strings.HasPrefix(socket, root) {
		t.Fatalf("socket %q is inside a root too long for it", socket)
	}
	endpoint, err := lifecycle.Listen(root)
	if err != nil {
		t.Fatal(err)
	}
	defer endpoint.Close()
	connection, err := net.DialTimeout("unix", socket, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	connection.Close()
	info, err := os.Stat(filepath.Dir(socket))
	if err != nil || info.Mode().Perm() != 0o700 {
		t.Fatalf("socket directory %v, %v; want one nobody else can reach", info, err)
	}
	if err := lifecycle.CheckSocketDirectory(socket); err != nil {
		t.Fatal(err)
	}
}

// Only this user may own the directory of a short socket: whoever owns it could answer as the host.
func TestADirectoryAnotherCouldReachIsNotTrustedForAShortSocket(t *testing.T) {
	socket := lifecycle.SocketPath(filepath.Join(t.TempDir(), strings.Repeat("r", 120)))
	directory := filepath.Dir(socket)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(directory, 0o755); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(directory, 0o700)
	if err := lifecycle.CheckSocketDirectory(socket); err == nil || !strings.Contains(err.Error(), directory) {
		t.Fatalf("an open directory: %v, want a refusal naming it", err)
	}
}

func TestRuntimePathPins(t *testing.T) {
	if got := lifecycle.SocketPath("/root/.vibesys"); got != "/root/.vibesys/host.sock" {
		t.Fatalf("SocketPath = %q", got)
	}
	root, err := os.MkdirTemp("", "lp")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	endpoint, err := lifecycle.Listen(root)
	if err != nil {
		t.Fatal(err)
	}
	defer endpoint.Close()
	if _, err := os.Stat(filepath.Join(root, "host.lock")); err != nil {
		t.Fatal(err)
	}
}

// This socket integration property proves exclusion never unpublishes a live listener.
func TestSecondListenPreservesFirstSocket(t *testing.T) {
	if err := quick.Check(func(rootID [4]byte) bool {
		root, err := os.MkdirTemp("", "l"+hex.EncodeToString(rootID[:]))
		if err != nil {
			t.Log(err)
			return false
		}
		defer os.RemoveAll(root)
		first, err := lifecycle.Listen(root)
		if err != nil {
			t.Log(err)
			return false
		}
		defer first.Close()
		second, err := lifecycle.Listen(root)
		if second != nil {
			second.Close()
			return false
		}
		if !errors.Is(err, lifecycle.ErrServing) {
			t.Log(err)
			return false
		}
		connection, err := net.DialTimeout("unix", lifecycle.SocketPath(root), 5*time.Second)
		if err != nil {
			t.Log(err)
			return false
		}
		defer connection.Close()
		if err := first.Listener.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
			t.Log(err)
			return false
		}
		accepted, err := first.Listener.Accept()
		if err != nil {
			t.Log(err)
			return false
		}
		accepted.Close()
		return true
	}, nil); err != nil {
		t.Fatal(err)
	}
}
