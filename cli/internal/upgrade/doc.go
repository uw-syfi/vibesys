// Package upgrade replaces the running vibesys-cli binary with the newest release.
//
// A release is a set of assets with stable, version-free names: one binary per supported platform
// (see AssetName), checksums.txt in sha256sum format, and VERSION. The same set is published
// under a tag per version and under the rolling cli-latest tag, so a client that wants the
// newest release never has to resolve a version first. cli/build_release.sh makes the set and
// .github/workflows/cli-release.yml publishes it; cli/install.sh reads the same names.
//
// The package is split the way the rest of the module is. The pure part (AssetName, ParseChecksums,
// ParseVersion) decides what to fetch and what to trust. Upgrade is the thin shell: it reads
// through a Source, stages the new binary next to the one it replaces, and renames it into place
// only after the bytes match the published checksum. A failure at any step leaves the installed
// binary exactly as it was.
package upgrade
