package upgrade_test

// The release build, upgrade and install.sh are three readers and writers of one layout. This
// test builds a real release with build_release.sh and holds the other two to it.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/upgrade"
)

func TestTheReleaseBuildMakesWhatUpgradeAndInstallRead(t *testing.T) {
	const version = "7.8.9-test.1"
	releases := t.TempDir()
	out := filepath.Join(releases, "cli-latest")

	ctx, cancel := context.WithTimeout(context.Background(), hangGuard)
	defer cancel()
	build := exec.CommandContext(ctx, filepath.Join(cliDir(t), "build_release.sh"), version, out)
	if combined, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build_release.sh: %v\n%s", err, combined)
	}

	// Exactly the assets Upgrade reads, and no others.
	want := []string{upgrade.ChecksumsAsset, upgrade.VersionAsset}
	for _, platform := range upgrade.Platforms {
		name, err := upgrade.AssetName(platform)
		must(t, err)
		want = append(want, name)
	}
	slices.Sort(want)
	if got := names(t, out); !slices.Equal(got, want) {
		t.Fatalf("assets = %v, want %v", got, want)
	}

	versionText, err := os.ReadFile(filepath.Join(out, upgrade.VersionAsset))
	must(t, err)
	if got, err := upgrade.ParseVersion(string(versionText)); err != nil || got != version {
		t.Fatalf("VERSION = %q (%v), want %q", versionText, err, version)
	}

	checksumsText, err := os.ReadFile(filepath.Join(out, upgrade.ChecksumsAsset))
	must(t, err)
	sums, err := upgrade.ParseChecksums(string(checksumsText))
	must(t, err)
	if len(sums) != len(upgrade.Platforms) {
		t.Fatalf("checksums.txt lists %d files, want %d", len(sums), len(upgrade.Platforms))
	}
	for _, platform := range upgrade.Platforms {
		name, _ := upgrade.AssetName(platform)
		content, err := os.ReadFile(filepath.Join(out, name))
		must(t, err)
		digest := sha256.Sum256(content)
		if sums[name] != hex.EncodeToString(digest[:]) {
			t.Errorf("%s: published digest %s is not the file's", name, sums[name])
		}
		// Static, and independent of the checkout it was built in.
		details, err := exec.CommandContext(ctx, "go", "version", "-m", filepath.Join(out, name)).Output()
		must(t, err)
		if !strings.Contains(string(details), "CGO_ENABLED=0") || strings.Contains(string(details), "vcs.") || !strings.Contains(string(details), "-trimpath=true") {
			t.Errorf("%s was not built static, VCS-free and trimmed:\n%s", name, details)
		}
	}

	// The binary for this machine reports the stamped version.
	hostAsset, err := upgrade.AssetName(upgrade.Platform{OS: runtime.GOOS, Arch: runtime.GOARCH})
	must(t, err)
	reported, err := exec.CommandContext(ctx, filepath.Join(out, hostAsset), "--version").Output()
	must(t, err)
	if got, want := string(reported), "vibesys-cli "+version+"\n"; got != want {
		t.Fatalf("--version = %q, want %q", got, want)
	}

	// install.sh installs it, for the platform this machine is.
	uname := machine{system: map[string]string{"linux": "Linux", "darwin": "Darwin"}[runtime.GOOS], arch: map[string]string{"amd64": "x86_64", "arm64": "arm64"}[runtime.GOARCH]}
	dir := filepath.Join(t.TempDir(), "bin")
	r := script{url: "file://" + releases, installDir: dir, machine: uname}.run(t)
	if !r.ok {
		t.Fatalf("install.sh: %s", r.errOut)
	}
	installedVersion, err := exec.CommandContext(ctx, filepath.Join(dir, "vibesys-cli"), "--version").Output()
	must(t, err)
	if string(installedVersion) != string(reported) {
		t.Fatalf("installed binary says %q, release binary says %q", installedVersion, reported)
	}
}
