package lifecycle_test

import (
	"encoding/hex"
	"errors"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"testing/quick"
	"time"

	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
)

func TestSocketDirectoryUsesHashPrefix(t *testing.T) {
	if err := quick.Check(func(suffix []byte) bool {
		prefix := strings.Repeat("a", lifecycle.HashPrefixLength)
		got := lifecycle.SocketPath("/root/.vibesys", prefix+string(suffix))
		return got == filepath.Join("/root/.vibesys", "gateway", prefix, "sock")
	}, nil); err != nil {
		t.Fatal(err)
	}
}

func TestSocketPathBound(t *testing.T) {
	limit := 108
	if runtime.GOOS == "darwin" {
		limit = 104
	}
	for _, length := range []int{limit - 1, limit, limit + 1} {
		path := "/" + strings.Repeat("x", length-1)
		err := lifecycle.ValidateSocketPath(path)
		if length < limit {
			if err != nil {
				t.Fatal(err)
			}
			continue
		}
		if err == nil || !strings.Contains(err.Error(), path) || !strings.Contains(err.Error(), strconv.Itoa(limit)) {
			t.Fatalf("ValidateSocketPath(%q) = %v", path, err)
		}
		if _, err := lifecycle.Listen(path, strings.Repeat("a", 64)); err == nil {
			t.Fatal("Listen accepted an oversized socket path")
		}
	}
}

func TestRuntimePathPins(t *testing.T) {
	const hash = "0123456789abcdef0123456789abcdef"
	if got := lifecycle.SocketPath("/root/.vibesys", hash); got != "/root/.vibesys/gateway/0123456789abcdef/sock" {
		t.Fatalf("SocketPath = %q", got)
	}
	root, err := os.MkdirTemp("", "lp")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	endpoint, err := lifecycle.Listen(root, hash)
	if err != nil {
		t.Fatal(err)
	}
	defer endpoint.Close()
	if _, err := os.Stat(filepath.Join(root, "gateway", "0123456789abcdef", "vibesysd.lock")); err != nil {
		t.Fatal(err)
	}
}

// This socket integration property proves exclusion never unpublishes a live listener.
func TestSecondListenPreservesFirstSocket(t *testing.T) {
	if err := quick.Check(func(rootID [4]byte, hashID [16]byte) bool {
		root, err := os.MkdirTemp("", "l"+hex.EncodeToString(rootID[:]))
		if err != nil {
			t.Log(err)
			return false
		}
		defer os.RemoveAll(root)
		hash := hex.EncodeToString(hashID[:])
		first, err := lifecycle.Listen(root, hash)
		if err != nil {
			t.Log(err)
			return false
		}
		defer first.Close()
		second, err := lifecycle.Listen(root, hash)
		if second != nil {
			second.Close()
			return false
		}
		if !errors.Is(err, lifecycle.ErrServing) {
			t.Log(err)
			return false
		}
		connection, err := net.DialTimeout("unix", lifecycle.SocketPath(root, hash), 5*time.Second)
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
