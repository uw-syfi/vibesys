package dockercli

import (
	"crypto/rand"
	"encoding/hex"

	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
)

// RandomIDs is a docker.IDs of random 64-bit ids. Two calls, in this process or another's, return
// the same id with a probability of one in 2^64.
type RandomIDs struct{}

var _ docker.IDs = RandomIDs{}

// New implements docker.IDs.
func (RandomIDs) New() string {
	var b [8]byte
	// Reading randomness does not fail: when the system cannot supply any, the program stops.
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}
