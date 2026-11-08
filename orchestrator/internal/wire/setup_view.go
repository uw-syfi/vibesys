package wire

import (
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// SetupCheckIDMessage is the contract's name for one of the setup checks.
func SetupCheckIDMessage(id core.SetupCheckID) pb.SetupCheckId { return setupChecks.out(id) }

// TaskCardMessage is the card a client shows of a task; nil for none.
func TaskCardMessage(c *core.TaskCard) *cliv1.TaskCard { return cardOut(c) }
