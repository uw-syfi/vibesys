package upgrade_test

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"testing/quick"

	"github.com/uw-syfi/vibesys/cli/internal/upgrade"
	"github.com/uw-syfi/vibesys/cli/internal/upgrade/upgradetest"
)

var host = upgrade.Platform{OS: "linux", Arch: "amd64"}

// install is a directory holding the binary to replace and a bystander, plus the release that
// would replace it.
type install struct {
	dir     string
	target  string
	release *upgradetest.Release
	binary  []byte
}

func newInstall(t *testing.T, binary []byte) install {
	t.Helper()
	dir := t.TempDir()
	must(t, os.WriteFile(filepath.Join(dir, "vibesys-cli"), []byte("old binary"), 0o755))
	must(t, os.WriteFile(filepath.Join(dir, "bystander"), []byte("untouched"), 0o644))
	binaries := map[upgrade.Platform][]byte{}
	for _, platform := range upgrade.Platforms {
		binaries[platform] = append([]byte(platform.OS+platform.Arch+":"), binary...)
	}
	binaries[host] = binary
	return install{dir: dir, target: filepath.Join(dir, "vibesys-cli"), release: upgradetest.New("2.0.0", binaries), binary: binary}
}

func (i install) upgrade() (upgrade.Result, error) {
	return upgrade.Upgrade(context.Background(), upgrade.Request{Source: i.release, Platform: host, Current: "1.0.0", Target: i.target})
}

// snapshot is every file in the install directory with its content and mode.
func (i install) snapshot(t *testing.T) map[string]string {
	t.Helper()
	entries, err := os.ReadDir(i.dir)
	must(t, err)
	files := map[string]string{}
	for _, entry := range entries {
		content, err := os.ReadFile(filepath.Join(i.dir, entry.Name()))
		must(t, err)
		info, err := entry.Info()
		must(t, err)
		files[entry.Name()] = info.Mode().String() + " " + string(content)
	}
	return files
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func sameFiles(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for name, content := range a {
		if b[name] != content {
			return false
		}
	}
	return true
}

func TestUpgradeInstallsTheNewestRelease(t *testing.T) {
	i := newInstall(t, []byte("new binary"))
	result, err := i.upgrade()
	must(t, err)
	if want := (upgrade.Result{Outcome: upgrade.Upgraded, From: "1.0.0", To: "2.0.0"}); result != want {
		t.Fatalf("result = %+v, want %+v", result, want)
	}
	want := map[string]string{"vibesys-cli": "-rwxr-xr-x new binary", "bystander": "-rw-r--r-- untouched"}
	if got := i.snapshot(t); !sameFiles(got, want) {
		t.Fatalf("directory = %v, want %v", got, want)
	}
}

func TestUpgradeFetchesThePlatformsOwnBinary(t *testing.T) {
	for _, platform := range upgrade.Platforms {
		i := newInstall(t, []byte("payload"))
		_, err := upgrade.Upgrade(context.Background(), upgrade.Request{Source: i.release, Platform: platform, Current: "1.0.0", Target: i.target})
		must(t, err)
		content, err := os.ReadFile(i.target)
		must(t, err)
		want := platform.OS + platform.Arch + ":payload"
		if platform == host {
			want = "payload"
		}
		if string(content) != want {
			t.Errorf("%v: installed %q, want %q", platform, content, want)
		}
	}
}

// A download cut short at any byte, even when the server calls it a clean end, is refused and
// leaves the installation as it was.
func TestAnyTruncatedDownloadIsRefused(t *testing.T) {
	property := func(binary []byte, cut uint) bool {
		if len(binary) == 0 {
			return true
		}
		i := newInstall(t, binary)
		name, _ := upgrade.AssetName(host)
		i.release.Inject(name, upgradetest.Fault{Truncate: true, TruncateAt: int(cut % uint(len(binary)))})
		before := i.snapshot(t)
		_, err := i.upgrade()
		return errors.Is(err, upgrade.ErrChecksumMismatch) && sameFiles(before, i.snapshot(t))
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

// Changing any one byte of the download is refused.
func TestAnyCorruptedDownloadIsRefused(t *testing.T) {
	property := func(binary []byte, at uint, flip byte) bool {
		if len(binary) == 0 || flip == 0 {
			return true
		}
		i := newInstall(t, binary)
		name, _ := upgrade.AssetName(host)
		corrupt := bytes.Clone(binary)
		corrupt[at%uint(len(corrupt))] ^= flip
		i.release.Set(name, corrupt)
		before := i.snapshot(t)
		_, err := i.upgrade()
		return errors.Is(err, upgrade.ErrChecksumMismatch) && sameFiles(before, i.snapshot(t))
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

// A connection that drops at any byte fails the upgrade with that failure and leaves nothing behind.
func TestAnyDroppedConnectionLeavesTheInstallationAsItWas(t *testing.T) {
	property := func(binary []byte, after uint) bool {
		i := newInstall(t, append([]byte("x"), binary...))
		name, _ := upgrade.AssetName(host)
		i.release.Inject(name, upgradetest.Fault{Fail: true, FailAfter: int(after % uint(len(binary)+2))})
		before := i.snapshot(t)
		_, err := i.upgrade()
		return errors.Is(err, upgradetest.ErrInjected) && sameFiles(before, i.snapshot(t))
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

func TestAnEmptyBinaryIsRefusedEvenWhenItsChecksumMatches(t *testing.T) {
	i := newInstall(t, []byte{})
	before := i.snapshot(t)
	if _, err := i.upgrade(); !errors.Is(err, upgrade.ErrChecksumMismatch) {
		t.Fatalf("error = %v, want ErrChecksumMismatch", err)
	}
	if !sameFiles(before, i.snapshot(t)) {
		t.Fatal("the installation changed")
	}
}

func TestTheSameVersionDownloadsNoBinary(t *testing.T) {
	i := newInstall(t, []byte("new binary"))
	before := i.snapshot(t)
	result, err := upgrade.Upgrade(context.Background(), upgrade.Request{Source: i.release, Platform: host, Current: "2.0.0", Target: i.target})
	must(t, err)
	if want := (upgrade.Result{Outcome: upgrade.UpToDate, From: "2.0.0", To: "2.0.0"}); result != want {
		t.Fatalf("result = %+v, want %+v", result, want)
	}
	if opened := i.release.Opened(); !slices.Equal(opened, []string{upgrade.VersionAsset}) {
		t.Fatalf("opened %v, want only %s", opened, upgrade.VersionAsset)
	}
	if !sameFiles(before, i.snapshot(t)) {
		t.Fatal("the installation changed")
	}
}

func TestADevelopmentBuildUpgradesToTheRelease(t *testing.T) {
	i := newInstall(t, []byte("new binary"))
	result, err := upgrade.Upgrade(context.Background(), upgrade.Request{Source: i.release, Platform: host, Current: "dev", Target: i.target})
	must(t, err)
	if result.Outcome != upgrade.Upgraded || result.From != "dev" {
		t.Fatalf("result = %+v, want an upgrade from dev", result)
	}
}

func TestAReleaseThatCannotBeTrustedChangesNothing(t *testing.T) {
	name, _ := upgrade.AssetName(host)
	cases := map[string]struct {
		damage func(*upgradetest.Release)
		want   error
	}{
		"no checksum for this platform": {func(r *upgradetest.Release) {
			r.Set(upgrade.ChecksumsAsset, []byte("0000000000000000000000000000000000000000000000000000000000000000  vibesys-cli_darwin_arm64\n"))
		}, upgrade.ErrNoChecksum},
		"checksums for different bytes": {func(r *upgradetest.Release) {
			r.Set(upgrade.ChecksumsAsset, []byte("0000000000000000000000000000000000000000000000000000000000000000  "+name+"\n"))
		}, upgrade.ErrChecksumMismatch},
		"checksums missing":     {func(r *upgradetest.Release) { r.Remove(upgrade.ChecksumsAsset) }, upgrade.ErrAssetNotFound},
		"binary missing":        {func(r *upgradetest.Release) { r.Remove(name) }, upgrade.ErrAssetNotFound},
		"version missing":       {func(r *upgradetest.Release) { r.Remove(upgrade.VersionAsset) }, upgrade.ErrAssetNotFound},
		"version not a version": {func(r *upgradetest.Release) { r.Set(upgrade.VersionAsset, []byte("latest\n")) }, nil},
		"checksums malformed":   {func(r *upgradetest.Release) { r.Set(upgrade.ChecksumsAsset, []byte("<html>rate limited</html>")) }, nil},
	}
	for label, c := range cases {
		t.Run(label, func(t *testing.T) {
			i := newInstall(t, []byte("new binary"))
			c.damage(i.release)
			before := i.snapshot(t)
			_, err := i.upgrade()
			if err == nil || (c.want != nil && !errors.Is(err, c.want)) {
				t.Fatalf("error = %v, want %v", err, c.want)
			}
			if !sameFiles(before, i.snapshot(t)) {
				t.Fatal("the installation changed")
			}
		})
	}
}

func TestAnUnsupportedPlatformOpensNothing(t *testing.T) {
	i := newInstall(t, []byte("new binary"))
	_, err := upgrade.Upgrade(context.Background(), upgrade.Request{Source: i.release, Platform: upgrade.Platform{OS: "plan9", Arch: "mips"}, Current: "1.0.0", Target: i.target})
	if !errors.Is(err, upgrade.ErrUnsupportedPlatform) {
		t.Fatalf("error = %v, want ErrUnsupportedPlatform", err)
	}
	if opened := i.release.Opened(); len(opened) != 0 {
		t.Fatalf("opened %v, want nothing", opened)
	}
}

// The last step failing, after the bytes were verified, still leaves no staged file behind.
func TestAFailedRenameLeavesNoStagedFile(t *testing.T) {
	i := newInstall(t, []byte("new binary"))
	// A directory cannot be replaced by renaming a file over it.
	must(t, os.Remove(i.target))
	must(t, os.Mkdir(i.target, 0o755))
	if _, err := i.upgrade(); err == nil {
		t.Fatal("upgrade over a directory succeeded")
	}
	entries, err := os.ReadDir(i.dir)
	must(t, err)
	var names []string
	for _, entry := range entries {
		names = append(names, entry.Name())
	}
	slices.Sort(names)
	if want := []string{"bystander", "vibesys-cli"}; !slices.Equal(names, want) {
		t.Fatalf("directory = %v, want %v", names, want)
	}
}

func TestAMissingInstallDirectoryIsAnErrorThatNamesIt(t *testing.T) {
	i := newInstall(t, []byte("new binary"))
	i.target = filepath.Join(i.dir, "gone", "vibesys-cli")
	_, err := i.upgrade()
	if err == nil || !bytes.Contains([]byte(err.Error()), []byte(filepath.Join(i.dir, "gone"))) {
		t.Fatalf("error = %v, want one naming %s", err, filepath.Join(i.dir, "gone"))
	}
}
