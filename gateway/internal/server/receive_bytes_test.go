package server_test

import (
	"github.com/uw-syfi/vibesys/gateway/internal/server"
	"testing"
)

func TestGatewayReceiveBudgetCoversOrchestratorFrames(t *testing.T) {
	// The orchestrator produces at most 4 MiB per response (snapshots at most
	// 1 MiB). The gateway serves that contract on the same HTTP/2 connection.
	const orchestratorFrameBytes = 4 << 20
	if server.MaxReceiveMessageBytes < orchestratorFrameBytes {
		t.Fatal("gateway receive limit is smaller than an orchestrator frame")
	}
}
