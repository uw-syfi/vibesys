package production

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Orchestrators with different state directories get different agent sockets in one runtime
// directory, each short enough for a Unix socket, and one state directory always gets the same.
func TestEachStateDirectoryHasItsOwnAgentSocket(t *testing.T) {
	root := t.TempDir()
	// A short runtime directory, as a real one is: a test directory may be too long for a socket.
	runtimeDir, err := os.MkdirTemp("", "vsr-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(runtimeDir) })
	seen := map[string]string{}
	for i := range 20 {
		state := filepath.Join(root, fmt.Sprintf("state-%d", i))
		if err := os.MkdirAll(state, 0o700); err != nil {
			t.Fatal(err)
		}
		env := map[string]string{"VIBESYS_ORCHESTRATOR_STATE_DIR": state, "XDG_RUNTIME_DIR": runtimeDir}
		layout, err := LayoutFor(func(k string) string { return env[k] }, root)
		if err != nil && !strings.Contains(err.Error(), "socket path") {
			t.Skipf("the test directory is not a local filesystem: %v", err)
		}
		if err != nil {
			t.Fatal(err)
		}
		again, err := LayoutFor(func(k string) string { return env[k] }, root)
		if err != nil {
			t.Fatal(err)
		}
		socket := layout.AgentSocket
		switch {
		case again.AgentSocket != socket:
			t.Fatalf("state %s got %s, then %s", state, socket, again.AgentSocket)
		case filepath.Dir(socket) != runtimeDir:
			t.Fatalf("state %s got socket %s outside the runtime directory %s", state, socket, runtimeDir)
		case len(socket) > maxSocketPath:
			t.Fatalf("socket %s is longer than %d bytes", socket, maxSocketPath)
		case seen[socket] != "":
			t.Fatalf("states %s and %s share the agent socket %s", seen[socket], state, socket)
		}
		seen[socket] = state
	}
}

// A second daemon on the same state directory is refused with an error that names the holder's pid
// and version, and the holder keeps serving.
func TestASecondDaemonNamesTheHolder(t *testing.T) {
	dir := t.TempDir()
	env := map[string]string{"VIBESYS_ORCHESTRATOR_STATE_DIR": dir}
	layout, err := LayoutFor(func(k string) string { return env[k] }, dir)
	if err != nil {
		t.Skipf("the test directory is not a local filesystem: %v", err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	first, err := Open(layout, Wiring{Sidecars: NoSidecars{}, Plans: NoPlans{}, Version: "1.2.3"}, log)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	_, err = Open(layout, Wiring{Sidecars: NoSidecars{}, Plans: NoPlans{}, Version: "9.9.9"}, log)
	want := fmt.Sprintf("pid %d version 1.2.3", os.Getpid())
	if !errors.Is(err, ErrHeld) || !strings.Contains(err.Error(), want) {
		t.Fatalf("second open gave %v, want ErrHeld naming %q", err, want)
	}
}

// The token secret is written whole or not at all, is stable once written, and a short file is
// never replaced, since replacing it would end the tokens of every live session.
func TestTheSecretIsStableAndAShortFileIsNeverReplaced(t *testing.T) {
	dir := t.TempDir()
	first, err := secretIn(dir)
	if err != nil || len(first) < 16 {
		t.Fatalf("first secret %d bytes, %v", len(first), err)
	}
	again, err := secretIn(dir)
	if err != nil || !bytes.Equal(first, again) {
		t.Fatalf("the secret changed between starts: %v", err)
	}
	if left, _ := filepath.Glob(filepath.Join(dir, "*.tmp")); len(left) != 0 {
		t.Fatalf("temporary files left behind: %v", left)
	}

	short := t.TempDir()
	path := filepath.Join(short, "token-secret")
	if err := os.WriteFile(path, []byte("half"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := secretIn(short); err == nil || !strings.Contains(err.Error(), path) {
		t.Fatalf("a short secret file gave %v, want an error naming %s", err, path)
	}
	if data, _ := os.ReadFile(path); string(data) != "half" {
		t.Fatalf("a short secret file was replaced with %q", data)
	}
}

// A server that stops, with or without an error, stops the daemon; the context ending is a clean stop.
func TestAServerThatStopsStopsTheDaemon(t *testing.T) {
	failed := make(chan error, 1)
	failed <- nil
	if err := awaitServers(context.Background(), failed); err == nil {
		t.Fatal("a server that returned nil left the daemon up")
	}
	boom := errors.New("accept failed")
	failed <- boom
	if err := awaitServers(context.Background(), failed); !errors.Is(err, boom) {
		t.Fatalf("got %v, want the server's error", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := awaitServers(ctx, make(chan error)); err != nil {
		t.Fatalf("a stop by the context reported %v", err)
	}
}
