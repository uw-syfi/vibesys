// Package buildinfo holds what a release build stamps into the gateway's binaries.
package buildinfo

import (
	"crypto/sha256"
	"encoding/hex"
	"io"
	"os"
)

// Version is the release version of this build: scripts/build_release_bundle.sh sets it with
// -ldflags "-X github.com/uw-syfi/vibesys/gateway/internal/buildinfo.Version=<version>", so every
// binary of a bundle reports the bundle's version. Any other build reports "dev".
var Version = "dev"

// Released says this build carries a release version, which then says all there is to say about which
// program it is. Any other build is told apart by ID.
func Released() bool { return Version != "dev" }

// ID is the identity of the program in the file at path, equal only for the same bytes. A client and a
// host are two programs, built from whatever the tree held at the time, so nothing built into them can
// be compared. What can be is the program a host is running against the one a client would start.
func ID(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	digest := sha256.New()
	if _, err := io.Copy(digest, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(digest.Sum(nil)), nil
}
