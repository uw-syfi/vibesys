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
