package wire

import (
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// SetupCheckIDMessage is the contract's name for one of the setup checks.
func SetupCheckIDMessage(id core.SetupCheckID) pb.SetupCheckId { return setupChecks.out(id) }
