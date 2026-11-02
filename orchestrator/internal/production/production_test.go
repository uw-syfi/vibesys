package production

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The agent socket is not the operator's, whether or not a runtime directory is set.
func TestTheTwoSocketsDiffer(t *testing.T) {
	for _, runtimeDir := range []string{"", "/run/user/1"} {
		operator, agent := socketPaths("/var/state/vo", runtimeDir)
		if operator == agent || operator == "" || agent == "" {
			t.Errorf("runtime directory %q: operator socket %q and agent socket %q must differ", runtimeDir, operator, agent)
		}
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
