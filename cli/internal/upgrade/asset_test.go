package upgrade_test

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"slices"
	"strings"
	"testing"
	"testing/quick"

	"github.com/uw-syfi/vibesys/cli/internal/upgrade"
)

func TestEverySupportedPlatformHasItsOwnVersionFreeAssetName(t *testing.T) {
	var names []string
	for _, platform := range upgrade.Platforms {
		name, err := upgrade.AssetName(platform)
		if err != nil {
			t.Fatal(err)
		}
		names = append(names, name)
	}
	want := []string{"vibesys-cli_linux_amd64", "vibesys-cli_linux_arm64", "vibesys-cli_darwin_amd64", "vibesys-cli_darwin_arm64"}
	if !slices.Equal(names, want) {
		t.Fatalf("asset names = %v, want %v", names, want)
	}
}

func TestAnyOtherPlatformIsUnsupported(t *testing.T) {
	property := func(goos, arch string) bool {
		if slices.Contains(upgrade.Platforms, upgrade.Platform{OS: goos, Arch: arch}) {
			return true
		}
		_, err := upgrade.AssetName(upgrade.Platform{OS: goos, Arch: arch})
		return errors.Is(err, upgrade.ErrUnsupportedPlatform)
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
	for _, platform := range []upgrade.Platform{{OS: "windows", Arch: "amd64"}, {OS: "linux", Arch: "riscv64"}, {}} {
		if _, err := upgrade.AssetName(platform); !errors.Is(err, upgrade.ErrUnsupportedPlatform) {
			t.Errorf("%v: error = %v, want ErrUnsupportedPlatform", platform, err)
		}
	}
}

func TestParseVersionAcceptsReleaseVersionsOnly(t *testing.T) {
	for _, good := range []string{"0.1.0", "10.20.30", "1.2.3-rc.1", "1.2.3-alpha-2"} {
		if got, err := upgrade.ParseVersion(good + "\n"); err != nil || got != good {
			t.Errorf("ParseVersion(%q) = %q, %v; want it accepted", good, got, err)
		}
		if got, err := upgrade.ParseVersion(good); err != nil || got != good {
			t.Errorf("ParseVersion(%q) without a newline = %q, %v; want it accepted", good, got, err)
		}
	}
	for _, bad := range []string{"", "dev", "v1.2.3", "1.2", "1.2.3.4", "1.2.3 ", " 1.2.3", "1.2.3\n\n", "1.2.3\nextra", "1.2.3-", "cli-v1.2.3"} {
		if got, err := upgrade.ParseVersion(bad); err == nil {
			t.Errorf("ParseVersion(%q) = %q, want an error", bad, got)
		}
	}
}

// Whatever the digests and names, the text sha256sum writes parses back to the same map, in both
// the text-mode and the binary-mode spelling.
func TestParseChecksumsReadsWhatSha256sumWrites(t *testing.T) {
	property := func(contents [][]byte, binaryMode bool) bool {
		want := map[string]string{}
		var text strings.Builder
		for i, content := range contents {
			sum := sha256.Sum256(content)
			name := "vibesys-cli_file" + strings.Repeat("x", i)
			want[name] = hex.EncodeToString(sum[:])
			separator := "  "
			if binaryMode {
				separator = " *"
			}
			text.WriteString(want[name] + separator + name + "\n")
		}
		got, err := upgrade.ParseChecksums(text.String())
		if err != nil || len(got) != len(want) {
			return false
		}
		for name, digest := range want {
			if got[name] != digest {
				return false
			}
		}
		return true
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

func TestParseChecksumsRefusesAnythingElse(t *testing.T) {
	digest := strings.Repeat("a", 64)
	for name, text := range map[string]string{
		"short digest":  strings.Repeat("a", 63) + "  file\n",
		"uppercase":     strings.Repeat("A", 64) + "  file\n",
		"no separator":  digest + "file\n",
		"one space":     digest + " \n",
		"prose":         "see the release page\n",
		"listed twice":  digest + "  file\n" + digest + "  file\n",
		"twice by mode": digest + "  file\n" + digest + " *file\n",
	} {
		if got, err := upgrade.ParseChecksums(text); err == nil {
			t.Errorf("%s: parsed %v, want an error", name, got)
		}
	}
}
