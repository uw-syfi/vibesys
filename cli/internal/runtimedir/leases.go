package runtimedir

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// LeaseParents are where the directory of a machine's leases may be, in the order they are tried:
// the lock directory of the file system hierarchy (memory-backed on Linux), /var/tmp, and the shared
// memory directory. The directory of a machine reached over ssh is found in the same order.
var LeaseParents = []string{"/run/lock", "/var/tmp", "/dev/shm"}

// LeaseDirName is the name of the directory of leases in its parent.
const LeaseDirName = "vibesys-leases"

// ResolveLeases is the directory of this machine's leases, where the daemons of every user of the
// machine take turns at what their jobs hold alone. It is vibesys-leases in the first of
// LeaseParents that is a directory every user may add to and nobody may remove another's from
// (sticky and writable by all, as /tmp is) and that is on local disk: a lock on a network file
// system can outlive its crashed holder until the server lets go, and a directory a network shares
// is shared with other machines, whose leases of the same names are other things.
//
// Which parent that is comes from the machine alone, never from the user or the environment, so
// every user's daemon finds the same directory. A machine with none of them has no directory of
// leases; the failure names each parent and why it is not one.
func ResolveLeases(fsys FS) (string, error) { return resolveLeases(fsys, LeaseParents) }

func resolveLeases(fsys FS, parents []string) (string, error) {
	var refused []string
	for _, p := range parents {
		info, err := os.Stat(p)
		switch {
		case err != nil:
			refused = append(refused, fmt.Sprintf("%s is not there", p))
			continue
		case !info.IsDir():
			refused = append(refused, fmt.Sprintf("%s is not a directory", p))
			continue
		case info.Mode()&(os.ModeSticky|0o777) != os.ModeSticky|0o777:
			refused = append(refused, fmt.Sprintf("%s is not sticky and writable by every user (mode %s)", p, info.Mode()))
			continue
		}
		network, err := fsys.IsNetwork(p)
		if err != nil {
			refused = append(refused, fmt.Sprintf("%s cannot be checked: %v", p, err))
			continue
		}
		if network {
			refused = append(refused, fmt.Sprintf("%s is on a network file system, where locks can outlive their holders and are shared with other machines", p))
			continue
		}
		return filepath.Join(p, LeaseDirName), nil
	}
	return "", &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
		"this machine has no local directory where every user's daemon can keep the leases of what its jobs hold alone: %s",
		strings.Join(refused, "; "))}
}
