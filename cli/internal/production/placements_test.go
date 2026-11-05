package production

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
)

// test-isolation: machineEnvironments is the wiring from the operator config to each placement's
// environment, which RunDaemon reaches only with a real runtime directory and index.

// Every placement of every kind is wired without reaching Docker or another machine, and offers the
// resources its section names, the AMD GPUs of an apptainer placement among them, with the claims
// its resources make.
func TestEveryPlacementKindIsWired(t *testing.T) {
	// The apptainer placements convert with this machine's apptainer, found on the daemon's PATH.
	bin := t.TempDir()
	if err := os.WriteFile(filepath.Join(bin, "apptainer"), []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	getenv := func(k string) string {
		if k == "PATH" {
			return bin
		}
		return ""
	}
	p := Process{UID: 1000, GID: 1000, Home: "/home/u", Executable: "/usr/bin/vibesys-cli", Getenv: getenv}
	for _, text := range configs {
		op := parse(t, text)
		m, err := p.machineEnvironments(op, clock.System{}, runtimedir.Dir{}, "key")
		if err != nil {
			t.Fatalf("config %q: %v", text, err)
		}
		if len(m.offers) != len(op.GetPlacements()) {
			t.Fatalf("config %q: offers %+v", text, m.offers)
		}
		for _, offer := range m.offers {
			pl := op.GetPlacements()[offer.Name]
			if rocm := pl.GetResources().GetRocm() != nil; rocm != slices.Contains(offer.Resources, core.ResourceROCm) {
				t.Errorf("config %q: placement %s offers %v", text, offer.Name, offer.Resources)
			}
		}
		if got, want := m.claims, claimsOf(t, text); len(want) > 0 && !sameClaims(got, want) {
			t.Errorf("config %q: claims %v, want %v", text, got, want)
		}
	}
}

// An apptainer placement on a machine without apptainer on the daemon's PATH is refused, saying to
// install it, and the placement's local scratch directory defaults under the user's cache.
func TestAnApptainerPlacementNeedsApptainerHere(t *testing.T) {
	p := Process{UID: 1000, GID: 1000, Home: "/home/u", Executable: "/usr/bin/vibesys-cli", Getenv: func(string) string { return "" }}
	op := parse(t, configs[len(configs)-2])
	_, err := p.machineEnvironments(op, clock.System{}, runtimedir.Dir{}, "key")
	if err == nil || !strings.Contains(err.Error(), "install Apptainer") {
		t.Errorf("without apptainer: %v", err)
	}
	for env, want := range map[string]string{"": "/home/u/.cache/vibesys-cli/apptainer", "/xdg": "/xdg/vibesys-cli/apptainer", "rel": "/home/u/.cache/vibesys-cli/apptainer"} {
		p.Getenv = func(k string) string {
			if k == "XDG_CACHE_HOME" {
				return env
			}
			return ""
		}
		if got := p.localScratch(nil); got != want {
			t.Errorf("XDG_CACHE_HOME=%q: %s, want %s", env, got, want)
		}
	}
}

func sameClaims(a, b map[string][]core.Claim) bool {
	for name, cs := range b {
		if !slices.Equal(a[name], cs) {
			return false
		}
	}
	return true
}
