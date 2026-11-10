package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/upgrade"
)

// downloadLimit bounds a whole upgrade, so a stalled connection ends it instead of hanging the
// caller. It guards against a hang and is not a deadline anything depends on.
const downloadLimit = 10 * time.Minute

// upgradeEnv is what an upgrade acts on: where releases come from, which binary to replace, and
// which version is running.
type upgradeEnv struct {
	Source   upgrade.Source
	Platform upgrade.Platform
	Current  string
	Target   string
	// Releases is the releases URL Source reads under, for messages.
	Releases string
}

// upgradeCommand adapts runUpgrade to the command table. An upgrade reaches no daemon.
func upgradeCommand(_ context.Context, d deps, args []string) int {
	return runUpgrade(args, d.stdout, d.stderr, d.upgrade)
}

// runUpgrade is `vibesys-cli upgrade`: it replaces this binary with the newest release. It takes
// a constructor for its environment so a test supplies a fake release and a temporary target.
func runUpgrade(args []string, stdout, stderr io.Writer, newEnv func() (upgradeEnv, error)) int {
	if len(args) != 0 {
		_, _ = fmt.Fprintln(stderr, "usage: vibesys-cli upgrade")
		return exitInvalidInput
	}
	env, err := newEnv()
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "vibesys-cli upgrade: %v\n", err)
		return exitFailed
	}
	result, err := upgrade.Upgrade(context.Background(), upgrade.Request{
		Source: env.Source, Platform: env.Platform, Current: env.Current, Target: env.Target,
	})
	if errors.Is(err, upgrade.ErrNoRelease) {
		_, _ = fmt.Fprintf(stderr, "vibesys-cli upgrade: %s\n", noReleaseMessage(env.Releases))
		return exitFailed
	}
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "vibesys-cli upgrade: %v\n", err)
		return exitFailed
	}
	switch result.Outcome {
	case upgrade.UpToDate:
		_, _ = fmt.Fprintf(stdout, "vibesys-cli %s is up to date\n", result.To)
	case upgrade.Upgraded:
		_, _ = fmt.Fprintf(stdout, "vibesys-cli upgraded from %s to %s\n", result.From, result.To)
	}
	return exitOK
}

// noReleaseMessage says that releases has no stable release, and how to install a version instead.
// It points to the list of releases only when releases is GitHub's, since a mirror's list is
// elsewhere. install.sh says the same.
func noReleaseMessage(releases string) string {
	releases = strings.TrimSuffix(releases, "/")
	listed := ""
	if releases == upgrade.DefaultReleasesURL {
		listed = " (versions are listed at " + upgrade.ReleasesPage + ")"
	}
	return fmt.Sprintf("no stable vibesys-cli release is published at %s/%s; to install a specific version, run install.sh with VIBESYS_CLI_VERSION=<version>%s",
		releases, upgrade.LatestTag, listed)
}

// productionUpgrade is the environment of the real binary: GitHub Releases, the running
// executable with its symlinks resolved, and this build's platform and version.
func productionUpgrade() (upgradeEnv, error) {
	releases := os.Getenv(upgrade.ReleasesURLEnv)
	if releases == "" {
		releases = upgrade.DefaultReleasesURL
	}
	source, err := upgrade.NewLatestSource(&http.Client{Timeout: downloadLimit}, releases)
	if err != nil {
		return upgradeEnv{}, err
	}
	executable, err := os.Executable()
	if err != nil {
		return upgradeEnv{}, fmt.Errorf("find the running binary: %w", err)
	}
	target, err := filepath.EvalSymlinks(executable)
	if err != nil {
		return upgradeEnv{}, fmt.Errorf("resolve %s: %w", executable, err)
	}
	return upgradeEnv{
		Source:   source,
		Platform: upgrade.Platform{OS: runtime.GOOS, Arch: runtime.GOARCH},
		Current:  version,
		Target:   target,
		Releases: releases,
	}, nil
}
