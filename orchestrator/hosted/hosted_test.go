package hosted_test

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/hosted"
)

// start opens an orchestrator over a fresh state directory with the given settings.
func start(t *testing.T, settings map[string]string) *hosted.Orchestrator {
	t.Helper()
	// A Unix socket path is short, so the directory is made under the temporary directory's root.
	state, err := os.MkdirTemp("", "h")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(state) })
	settings["VIBESYS_ORCHESTRATOR_STATE_DIR"] = state
	ctx, cancel := context.WithCancel(context.Background())
	x := hosted.Start(ctx, hosted.Options{
		Version: "test", Executable: filepath.Join(state, "bin", "vibesysd"), Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Getenv: func(k string) string { return settings[k] }, Home: state,
	})
	t.Cleanup(func() {
		cancel()
		<-x.Done()
	})
	return x
}

// A CLI setting that names no executable stops the orchestrator from starting and says which
// setting is wrong, so a run never starts with a build nobody chose.
func TestACLISettingThatNamesNoExecutableIsRefusedAtStart(t *testing.T) {
	for _, setting := range []string{"vibesys-cli", "/missing/vibesys-cli"} {
		x := start(t, map[string]string{"VIBESYS_ORCHESTRATOR_CLI": setting})
		if !strings.Contains(x.Reason(), "VIBESYS_ORCHESTRATOR_CLI") {
			t.Errorf("setting %q: reason %q, want one naming the setting", setting, x.Reason())
		}
	}
}

// With no setting, no build beside the daemon and none on its PATH, the orchestrator does not start:
// the skills have no PATH fallback, so a run could not work.
func TestNoCLIToGiveTheAgentsStopsTheOrchestratorFromStarting(t *testing.T) {
	x := start(t, map[string]string{"PATH": "/nonexistent"})
	if !strings.Contains(x.Reason(), "VIBESYS_ORCHESTRATOR_CLI") {
		t.Fatalf("reason %q, want one naming the setting", x.Reason())
	}
}

func TestAnExecutableCLISettingStartsTheOrchestrator(t *testing.T) {
	cli := filepath.Join(t.TempDir(), "vibesys-cli")
	if err := os.WriteFile(cli, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	x := start(t, map[string]string{"VIBESYS_ORCHESTRATOR_CLI": cli})
	if x.Reason() != "" {
		t.Fatalf("not served: %s", x.Reason())
	}
}
