// Package buildinfo holds what a release build stamps into the gateway's binaries.
package buildinfo

// Version is the release version of this build: scripts/build_release_bundle.sh sets it with
// -ldflags "-X github.com/uw-syfi/vibesys/gateway/internal/buildinfo.Version=<version>", so every
// binary of a bundle reports the bundle's version. Any other build reports "dev".
var Version = "dev"
