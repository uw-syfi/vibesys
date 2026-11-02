package runtimedir_test

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
)

type fakeFS struct{ network bool }

func (f fakeFS) IsNetwork(string) (bool, error) { return f.network, nil }

func shortDir(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "vsr")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	return dir
}

func TestResolve(t *testing.T) {
	env := func(m map[string]string) func(string) string { return func(k string) string { return m[k] } }
	for _, tc := range []struct {
		name string
		env  map[string]string
		want string
	}{
		{"runtime dir set", map[string]string{"XDG_RUNTIME_DIR": "/run/user/7"}, "/run/user/7/vibesys-cli"},
		{"unset falls back to a private local directory", nil, "/tmp/vibesys-cli-7"},
		{"a relative value is not trusted", map[string]string{"XDG_RUNTIME_DIR": "run"}, "/tmp/vibesys-cli-7"},
	} {
		if got := runtimedir.Resolve(env(tc.env), 7); got != tc.want {
			t.Errorf("%s: Resolve = %q, want %q", tc.name, got, tc.want)
		}
	}
}

func TestOpenMakesAPrivateDirectory(t *testing.T) {
	path := filepath.Join(shortDir(t), "rt")
	d, err := runtimedir.Open(path, os.Getuid(), fakeFS{})
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(d.Path())
	if err != nil || info.Mode().Perm() != 0o700 {
		t.Fatalf("directory mode %v, error %v; want 0700", info.Mode().Perm(), err)
	}
	if _, err := os.Stat(d.Supervisors()); err != nil {
		t.Fatalf("no supervisor directory: %v", err)
	}
}

func TestOpenMakesAnOpenDirectoryPrivate(t *testing.T) {
	path := filepath.Join(shortDir(t), "rt")
	if err := os.Mkdir(path, 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := runtimedir.Open(path, os.Getuid(), fakeFS{}); err != nil {
		t.Fatal(err)
	}
	if info, _ := os.Stat(path); info.Mode().Perm() != 0o700 {
		t.Fatalf("mode = %v, want 0700", info.Mode().Perm())
	}
}

func TestOpenRefusesWhatIsNotTheUsers(t *testing.T) {
	base := shortDir(t)
	path := filepath.Join(base, "rt")
	if err := os.Mkdir(path, 0o700); err != nil {
		t.Fatal(err)
	}
	if _, err := runtimedir.Open(path, os.Getuid()+1, fakeFS{}); err == nil {
		t.Error("a directory of another user was accepted")
	}
	link := filepath.Join(base, "link")
	if err := os.Symlink(path, link); err != nil {
		t.Fatal(err)
	}
	if _, err := runtimedir.Open(link, os.Getuid(), fakeFS{}); err == nil {
		t.Error("a link was accepted as the runtime directory")
	}
	if _, err := runtimedir.Open("relative", os.Getuid(), fakeFS{}); err == nil {
		t.Error("a relative path was accepted")
	}
}

func TestOpenRefusesANetworkFileSystemAndSaysHow(t *testing.T) {
	path := filepath.Join(shortDir(t), "rt")
	_, err := runtimedir.Open(path, os.Getuid(), fakeFS{network: true})
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureUnavailable || !strings.Contains(f.Message, "XDG_RUNTIME_DIR") || !strings.Contains(f.Message, path) {
		t.Fatalf("error = %v, want unavailable naming the path and XDG_RUNTIME_DIR", err)
	}
}

func TestOpenRefusesAPathTooLongForASocket(t *testing.T) {
	path := filepath.Join(shortDir(t), strings.Repeat("d", 90))
	_, err := runtimedir.Open(path, os.Getuid(), fakeFS{})
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "Unix socket") {
		t.Fatalf("error = %v, want invalid input about the socket path", err)
	}
}

func TestSystemFSKnowsALocalDirectory(t *testing.T) {
	network, err := runtimedir.System().IsNetwork(shortDir(t))
	if err != nil || network {
		t.Fatalf("IsNetwork(temp dir) = %v, %v; want false", network, err)
	}
}

func TestTryLockExcludesAndReleases(t *testing.T) {
	path := filepath.Join(shortDir(t), "x.lock")
	first, err := runtimedir.TryLock(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := runtimedir.TryLock(path); !errors.Is(err, runtimedir.ErrHeld) {
		t.Fatalf("second TryLock = %v, want ErrHeld", err)
	}
	first.Unlock()
	again, err := runtimedir.TryLock(path)
	if err != nil {
		t.Fatalf("TryLock after Unlock: %v", err)
	}
	again.Unlock()
}

func TestWaitLockWaitsForTheHolder(t *testing.T) {
	path := filepath.Join(shortDir(t), "x.lock")
	held, err := runtimedir.TryLock(path)
	if err != nil {
		t.Fatal(err)
	}
	got := make(chan error, 1)
	go func() {
		l, err := runtimedir.WaitLock(context.Background(), path)
		if err == nil {
			l.Unlock()
		}
		got <- err
	}()
	held.Unlock()
	if err := <-got; err != nil {
		t.Fatalf("WaitLock: %v", err)
	}
}

func TestWaitLockGivesUpWithItsContext(t *testing.T) {
	path := filepath.Join(shortDir(t), "x.lock")
	held, err := runtimedir.TryLock(path)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := runtimedir.WaitLock(ctx, path); !errors.Is(err, context.Canceled) {
		t.Fatalf("WaitLock = %v, want the context's error", err)
	}
	// The abandoned wait must not keep the lock once the holder lets go. It may take the lock for
	// an instant on its way out, so the check waits for the lock and not for a moment; a wait
	// that kept the lock would never return, and the backstop is what fails the test then.
	held.Unlock()
	backstop, stop := context.WithTimeout(context.Background(), time.Minute)
	defer stop()
	l, err := runtimedir.WaitLock(backstop, path)
	if err != nil {
		t.Fatalf("the abandoned wait kept the lock: %v", err)
	}
	l.Unlock()
}
