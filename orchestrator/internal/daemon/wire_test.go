package daemon_test

import (
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

func wireRun(r core.Run) *pb.Run       { return wire.RunMessage(r) }
func wireAgent(a core.Agent) *pb.Agent { return wire.AgentMessage(a) }
