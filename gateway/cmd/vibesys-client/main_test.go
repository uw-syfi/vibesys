package main_test

import (
	"bytes"
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	"github.com/uw-syfi/vibesys/gateway/internal/server"
)

// stamped is the release version the test builds the client with, as scripts/build_release_bundle.sh
// does, so what the client reports can only come from the build.
const stamped = "7.8.9-test"

func build(t *testing.T) string {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	dir := t.TempDir()
	binary := filepath.Join(dir, "vibesys-client")
	if output, err := exec.CommandContext(ctx, "go", "build", "-ldflags", "-X github.com/uw-syfi/vibesys/gateway/internal/buildinfo.Version="+stamped, "-o", binary, ".").CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, output)
	}
	// The host is only looked for, never started, while a gateway already serves the socket.
	if err := os.WriteFile(filepath.Join(dir, "vibesys-host"), []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	return binary
}

func run(t *testing.T, binary string, args ...string) (code int, stdout, stderr string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	var out, errb bytes.Buffer
	command := exec.CommandContext(ctx, binary, args...)
	command.Stdout, command.Stderr = &out, &errb
	var exit *exec.ExitError
	switch err := command.Run(); {
	case err == nil:
	case errors.As(err, &exit):
		code = exit.ExitCode()
	default:
		t.Fatal(err)
	}
	return code, out.String(), errb.String()
}

// The exit code of the real binary is the verbs' exit code: usage 2, and a peer that serves the
// host API but not the orchestrator is unavailable, 4.
func TestClientCLIExitCodes(t *testing.T) {
	binary := build(t)
	if code, _, stderr := run(t, binary); code != 2 || !strings.Contains(stderr, "usage: vibesys-client") {
		t.Errorf("no arguments: exit %d, stderr %q", code, stderr)
	}
	if code, stdout, _ := run(t, binary, "--help"); code != 0 || !strings.Contains(stdout, "run start") {
		t.Errorf("--help: exit %d, stdout %q", code, stdout)
	}
	if code, stdout, _ := run(t, binary, "--version"); code != 0 || stdout != "vibesys-client "+stamped+"\n" {
		t.Errorf("--version: exit %d, stdout %q, want the stamped version", code, stdout)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	root, err := os.MkdirTemp("", "g")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	endpoint, err := lifecycle.Listen(root)
	if err != nil {
		t.Fatal(err)
	}
	defer endpoint.Close()
	served := make(chan error, 1)
	go func() { served <- server.Serve(ctx, endpoint.Listener, server.Config{Root: root, Version: "cli-test"}) }()
	defer func() {
		cancel()
		if err := <-served; err != nil {
			t.Error(err)
		}
	}()
	code, stdout, stderr := run(t, binary, "--root", root, "run", "list")
	if code != 4 || stdout != "" || !strings.Contains(stderr, "vibesys-client:") {
		t.Errorf("run list against a host with no orchestrator: exit %d, stdout %q, stderr %q", code, stdout, stderr)
	}
}
