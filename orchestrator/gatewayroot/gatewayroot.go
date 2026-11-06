// Package gatewayroot says which host .vibesys directory a gateway serves from, so that the host
// binary and its clients, which are separate programs, agree on it without sharing anything else.
package gatewayroot

import (
	"errors"
	"fmt"
	"path/filepath"

	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
)

// For is the host .vibesys directory a gateway serves from and its clients dial, so that a
// gateway belongs to exactly one orchestrator state directory. In order:
//
//  1. The --root flag, when given.
//  2. A subdirectory of the state directory when VIBESYS_ORCHESTRATOR_STATE_DIR is set, so a
//     client whose state is elsewhere never reaches a gateway (and the orchestrator inside it) that
//     serves another state, and never starts one under the default root. A relative setting is an
//     error naming it.
//  3. ~/.vibesys.
//
// The gateway's socket and the host's run files live under the root; the orchestrator's own state
// stays in the state directory, and a root derived from it is a separate subdirectory.
func For(flag string, getenv func(string) string, home string) (string, error) {
	switch state := getenv(localfs.EnvStateDir); {
	case flag != "":
		return filepath.Abs(flag)
	case state != "":
		if !filepath.IsAbs(state) {
			return "", fmt.Errorf("%s=%q must be an absolute path", localfs.EnvStateDir, state)
		}
		return filepath.Join(filepath.Clean(state), derivedRootName), nil
	case home == "":
		return "", errors.New("no home directory for the default root ~/.vibesys; pass --root or set " + localfs.EnvStateDir)
	default:
		return filepath.Join(home, ".vibesys"), nil
	}
}

// derivedRootName is the subdirectory of a state directory that is the gateway root derived from it.
const derivedRootName = "host"
