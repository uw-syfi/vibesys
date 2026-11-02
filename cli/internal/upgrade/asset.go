package upgrade

import (
	"bufio"
	"errors"
	"fmt"
	"regexp"
	"strings"
)

// Names of the assets that are not binaries.
const (
	// ChecksumsAsset lists the SHA-256 of every binary, one "<hex>  <name>" line each.
	ChecksumsAsset = "checksums.txt"
	// VersionAsset holds the release version and nothing else.
	VersionAsset = "VERSION"
)

// Platform is an operating system and architecture, spelled the way Go spells them.
type Platform struct {
	OS   string
	Arch string
}

// Platforms are the targets a release carries a binary for.
var Platforms = []Platform{
	{OS: "linux", Arch: "amd64"},
	{OS: "linux", Arch: "arm64"},
	{OS: "darwin", Arch: "amd64"},
	{OS: "darwin", Arch: "arm64"},
}

// ErrUnsupportedPlatform means no release binary exists for the platform.
var ErrUnsupportedPlatform = errors.New("no release binary for this platform")

// AssetName is the name of the release binary for p. It carries no version, so
// releases/latest/download-style URLs work.
func AssetName(p Platform) (string, error) {
	for _, known := range Platforms {
		if known == p {
			return "vibesys-cli_" + p.OS + "_" + p.Arch, nil
		}
	}
	return "", fmt.Errorf("%w: %s/%s", ErrUnsupportedPlatform, p.OS, p.Arch)
}

var versionPattern = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$`)

// ParseVersion returns the version in the text of a VERSION asset, or an error when the text is
// not one version on one line. A release version is MAJOR.MINOR.PATCH with an optional
// pre-release suffix and no leading "v".
func ParseVersion(text string) (string, error) {
	version := strings.TrimSuffix(text, "\n")
	if !versionPattern.MatchString(version) {
		return "", fmt.Errorf("release version %q is not MAJOR.MINOR.PATCH[-suffix]", version)
	}
	return version, nil
}

var checksumLine = regexp.MustCompile(`^([0-9a-f]{64}) [ *]([^ ].*)$`)

// ParseChecksums reads sha256sum output into a map from file name to lowercase hex digest. It
// rejects a malformed line and a name listed twice, because either means the file is not the one
// the release published.
func ParseChecksums(text string) (map[string]string, error) {
	sums := map[string]string{}
	scanner := bufio.NewScanner(strings.NewReader(text))
	for number := 1; scanner.Scan(); number++ {
		line := scanner.Text()
		if line == "" {
			continue
		}
		match := checksumLine.FindStringSubmatch(line)
		if match == nil {
			return nil, fmt.Errorf("%s line %d is not a sha256sum line", ChecksumsAsset, number)
		}
		if _, dup := sums[match[2]]; dup {
			return nil, fmt.Errorf("%s lists %s twice", ChecksumsAsset, match[2])
		}
		sums[match[2]] = match[1]
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("read %s: %w", ChecksumsAsset, err)
	}
	return sums, nil
}
