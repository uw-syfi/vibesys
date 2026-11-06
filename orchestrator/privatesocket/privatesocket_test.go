package privatesocket_test

import (
	"net"
	"os"
	"path/filepath"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/privatesocket"
)

func shortDirectory(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "p")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	return dir
}

// A socket answers at its path with mode 0600, nothing else is left beside it, and it replaces what a
// crash left there.
func TestASocketIsPrivateAndAnswersAtItsPath(t *testing.T) {
	dir := shortDirectory(t)
	path := filepath.Join(dir, "host.sock")
	for range 3 { // Each round finds the name the last one left, as after a crash.
		listener, err := privatesocket.Listen(path)
		if err != nil {
			t.Fatal(err)
		}
		info, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode()&os.ModeSocket == 0 || info.Mode().Perm() != 0o600 {
			t.Fatalf("the socket has mode %v", info.Mode())
		}
		connection, err := net.Dial("unix", path)
		if err != nil {
			t.Fatal(err)
		}
		connection.Close()
		entries, err := os.ReadDir(dir)
		if err != nil || len(entries) != 1 {
			t.Fatalf("beside the socket: %v, %v", entries, err)
		}
		if err := listener.Close(); err != nil {
			t.Fatal(err)
		}
	}
	if err := privatesocket.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := privatesocket.Remove(path); err != nil {
		t.Fatalf("removing a name already gone: %v", err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("the name is still there: %v", err)
	}
}

// Listening changes nothing for the rest of the process: a directory another goroutine makes meanwhile
// has the mode it asked for. A narrowed umask gave it no execute bit, which failed unrelated work with
// "permission denied".
func TestListeningLeavesTheRestOfTheProcessAlone(t *testing.T) {
	dir := shortDirectory(t)
	done := make(chan struct{})
	var others sync.WaitGroup
	others.Add(1)
	go func() {
		defer others.Done()
		for i := 0; ; i++ {
			select {
			case <-done:
				return
			default:
			}
			made, err := os.MkdirTemp(dir, "d")
			if err != nil {
				t.Error(err)
				return
			}
			info, err := os.Stat(made)
			if err != nil {
				t.Error(err)
				return
			}
			if info.Mode().Perm() != 0o700 {
				t.Errorf("a directory made during a listen has mode %v", info.Mode().Perm())
				return
			}
			os.Remove(made)
		}
	}()
	path := filepath.Join(dir, "host.sock")
	for range 300 {
		listener, err := privatesocket.Listen(path)
		if err != nil {
			t.Error(err)
			break
		}
		listener.Close()
	}
	close(done)
	others.Wait()
}
