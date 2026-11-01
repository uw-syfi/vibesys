package main

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/upgrade"
	"github.com/uw-syfi/vibesys/cli/internal/upgrade/upgradetest"
)

var hostPlatform = upgrade.Platform{OS: "linux", Arch: "arm64"}

// installation is a temporary binary with a release one version ahead of it.
type installation struct {
	target  string
	release *upgradetest.Release
}

func newInstallation(t *testing.T, newBinary string) installation {
	t.Helper()
	target := filepath.Join(t.TempDir(), "vibesys-cli")
	if err := os.WriteFile(target, []byte("old binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	return installation{target: target, release: upgradetest.New("2.0.0", map[upgrade.Platform][]byte{hostPlatform: []byte(newBinary)})}
}

func (i installation) env(current string) func() (upgradeEnv, error) {
	return func() (upgradeEnv, error) {
		return upgradeEnv{Source: i.release, Platform: hostPlatform, Current: current, Target: i.target}, nil
	}
}

func (i installation) content(t *testing.T) string {
	t.Helper()
	data, err := os.ReadFile(i.target)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestUpgradeReplacesTheBinaryAndSaysSo(t *testing.T) {
	i := newInstallation(t, "new binary")
	var stdout, stderr bytes.Buffer
	if code := runUpgrade(nil, &stdout, &stderr, i.env("1.0.0")); code != exitOK {
		t.Fatalf("exit code = %d, stderr = %q", code, stderr.String())
	}
	if got, want := stdout.String(), "vibesys-cli upgraded from 1.0.0 to 2.0.0\n"; got != want {
		t.Fatalf("stdout = %q, want %q", got, want)
	}
	if stderr.Len() != 0 || i.content(t) != "new binary" {
		t.Fatalf("stderr = %q, binary = %q", stderr.String(), i.content(t))
	}
}

func TestUpgradeOfTheNewestVersionChangesNothing(t *testing.T) {
	i := newInstallation(t, "new binary")
	var stdout, stderr bytes.Buffer
	if code := runUpgrade(nil, &stdout, &stderr, i.env("2.0.0")); code != exitOK {
		t.Fatalf("exit code = %d, stderr = %q", code, stderr.String())
	}
	if got, want := stdout.String(), "vibesys-cli 2.0.0 is up to date\n"; got != want {
		t.Fatalf("stdout = %q, want %q", got, want)
	}
	if i.content(t) != "old binary" {
		t.Fatalf("binary = %q, want it unchanged", i.content(t))
	}
}

// A download that fails verification is an error: nothing on stdout, the reason on stderr, and
// the installed binary untouched.
func TestUpgradeRefusesADamagedDownload(t *testing.T) {
	i := newInstallation(t, "new binary")
	name, _ := upgrade.AssetName(hostPlatform)
	i.release.Inject(name, upgradetest.Fault{Truncate: true, TruncateAt: 4})
	var stdout, stderr bytes.Buffer
	if code := runUpgrade(nil, &stdout, &stderr, i.env("1.0.0")); code != exitFailure {
		t.Fatalf("exit code = %d, want %d", code, exitFailure)
	}
	if stdout.Len() != 0 || !strings.Contains(stderr.String(), "checksum") {
		t.Fatalf("stdout = %q, stderr = %q", stdout.String(), stderr.String())
	}
	if i.content(t) != "old binary" {
		t.Fatalf("binary = %q, want it unchanged", i.content(t))
	}
}

func TestUpgradeReportsAnEnvironmentItCannotBuild(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := runUpgrade(nil, &stdout, &stderr, func() (upgradeEnv, error) { return upgradeEnv{}, errors.New("no home") })
	if code != exitFailure || stdout.Len() != 0 || !strings.Contains(stderr.String(), "no home") {
		t.Fatalf("code = %d, stdout = %q, stderr = %q", code, stdout.String(), stderr.String())
	}
}

func TestUpgradeTakesNoArguments(t *testing.T) {
	i := newInstallation(t, "new binary")
	var stdout, stderr bytes.Buffer
	if code := runUpgrade([]string{"--force"}, &stdout, &stderr, i.env("1.0.0")); code != exitInvalidInput {
		t.Fatalf("exit code = %d, want %d", code, exitInvalidInput)
	}
	if stdout.Len() != 0 || stderr.Len() == 0 || i.content(t) != "old binary" {
		t.Fatalf("stdout = %q, stderr = %q, binary = %q", stdout.String(), stderr.String(), i.content(t))
	}
}

func TestRunRoutesUpgradeToItsCommand(t *testing.T) {
	t.Setenv(upgrade.ReleasesURLEnv, "http://insecure.invalid")
	var stdout, stderr bytes.Buffer
	if code := run([]string{"upgrade"}, &stdout, &stderr); code != exitFailure {
		t.Fatalf("exit code = %d, want %d", code, exitFailure)
	}
	if !strings.Contains(stderr.String(), upgrade.ReleasesURLEnv) {
		t.Fatalf("stderr = %q, want it to name %s", stderr.String(), upgrade.ReleasesURLEnv)
	}
}
