package lifecycle_test

import (
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"testing/quick"

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
