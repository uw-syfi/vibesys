package production

import (
	"errors"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
)

// StateRefusalOf says what is wrong with the place of a refused state directory, so that a client
// names the setting that fixes it. Every other refusal (a path too long for its sockets, one that
// cannot be made) has no such answer and stays unspecified.
func StateRefusalOf(err error) pb.StateRefusal {
	var refused *localfs.RefusedError
	switch {
	case !errors.As(err, &refused):
		return pb.StateRefusal_STATE_REFUSAL_UNSPECIFIED
	case refused.Filesystem.Class == localfs.Network:
		return pb.StateRefusal_STATE_REFUSAL_NETWORK_FILESYSTEM
	}
	return pb.StateRefusal_STATE_REFUSAL_UNKNOWN_FILESYSTEM
}
