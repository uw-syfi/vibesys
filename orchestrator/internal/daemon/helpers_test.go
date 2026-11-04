package daemon_test

import (
	"context"
	"fmt"

	"google.golang.org/grpc/metadata"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

func agentContext(agent core.AgentID, session uint32, token string) context.Context {
	return metadata.NewIncomingContext(context.Background(), metadata.Pairs(
		daemon.MetaAgent, string(agent), daemon.MetaSession, fmt.Sprint(session), daemon.MetaToken, token))
}
