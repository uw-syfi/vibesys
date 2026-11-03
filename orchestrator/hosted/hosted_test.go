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

// getenv answers from a fixed set of settings.
func getenv(settings map[string]string) func(string) string {
	return func(k string) string { return settings[k] }
}

// A gateway root is a function of the flag and the state directory alone: the flag wins; else a
// state directory gives a root under it, one per state directory, never the default; else
// ~/.vibesys. So two state directories never share a gateway, whatever the home is.
func TestTheGatewayRootFollowsTheFlagThenTheStateDirectoryThenTheDefault(t *testing.T) {
	const stateEnv = "VIBESYS_ORCHESTRATOR_STATE_DIR"
	states := []string{"", "/s/a", "/s/b", "/s/a/", "/s/../s/a", "/h/.vibesys"}
	flags := []string{"", "/f/root", "/s/a/host"}
	homes := []string{"/h", "/other"}
	seen := map[string]string{}
	for _, state := range states {
		for _, flag := range flags {
			for _, home := range homes {
				got, err := hosted.GatewayRoot(flag, getenv(map[string]string{stateEnv: state}), home)
				if err != nil {
					t.Fatalf("flag %q state %q home %q: %v", flag, state, home, err)
				}
				switch {
				case flag != "":
					if got != flag {
						t.Errorf("flag %q state %q: got %q, want the flag", flag, state, got)
					}
				case state == "":
					if want := filepath.Join(home, ".vibesys"); got != want {
						t.Errorf("no state, home %q: got %q, want %q", home, got, want)
					}
				default:
					if got == filepath.Join(home, ".vibesys") || !strings.HasPrefix(got, filepath.Clean(state)+"/") {
						t.Errorf("state %q home %q: root %q is not under the state directory", state, home, got)
					}
					if prev, ok := seen[filepath.Clean(state)]; ok && prev != got {
						t.Errorf("state %q: roots %q and %q for one state directory", state, prev, got)
					}
					seen[filepath.Clean(state)] = got
				}
			}
		}
	}
	if seen["/s/a"] == seen["/s/b"] {
		t.Errorf("two state directories share the root %q", seen["/s/a"])
	}
}

// A state directory that is not absolute is an error naming the setting, never a root relative to the
// working directory; with no state directory and no home there is nothing to default to.
func TestTheGatewayRootRefusesWhatItCannotPlaceWithoutGuessing(t *testing.T) {
	_, err := hosted.GatewayRoot("", getenv(map[string]string{"VIBESYS_ORCHESTRATOR_STATE_DIR": "state"}), "/h")
	if err == nil || !strings.Contains(err.Error(), "VIBESYS_ORCHESTRATOR_STATE_DIR") {
		t.Errorf("relative state directory: %v, want an error naming the setting", err)
	}
	if _, err := hosted.GatewayRoot("", getenv(nil), ""); err == nil {
		t.Error("no home and no state directory: want an error")
	}
	if got, err := hosted.GatewayRoot("/f", getenv(nil), ""); err != nil || got != "/f" {
		t.Errorf("a flag needs no home: got %q, %v", got, err)
	}
}
