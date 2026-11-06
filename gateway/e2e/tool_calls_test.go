//go:build e2e

package e2e

import (
	"context"
	"strings"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// toolCalls reads an agent's tool calls through the operator's service, as a client does.
func (r *rig) toolCalls(agent string) []*pb.ToolCall {
	r.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), bound)
	defer cancel()
	connection := r.dial(ctx)
	defer connection.Close()
	response, err := pb.NewOrchestratorServiceClient(connection).ListItems(ctx, &pb.ListItemsRequest{AgentId: agent, BeforeOrdinal: 1 << 30, PageSize: 1000})
	if err != nil {
		r.t.Fatalf("list the items of %s: %v", agent, err)
	}
	var calls []*pb.ToolCall
	for _, item := range response.GetPage().GetItems() {
		if call := item.GetTool(); call != nil {
			calls = append(calls, call)
		}
	}
	return calls
}

// checkToolCalls holds an agent's calls to what a client shows: every call is one typed case, the
// commands the script ran are commands, and the orchestrator's tools are named by the tool, on both
// harnesses, whatever the provider called them.
func checkToolCalls(t *testing.T, who string, calls []*pb.ToolCall, mcpTool string) {
	t.Helper()
	var commands, named int
	for _, call := range calls {
		switch c := call.GetCall().(type) {
		case nil:
			t.Errorf("%s has a tool call with no case: %v", who, call)
		case *pb.ToolCall_Shell:
			if strings.HasPrefix(c.Shell.GetCommand(), "vibesys-cli") || strings.HasPrefix(c.Shell.GetCommand(), "git") {
				commands++
			}
		case *pb.ToolCall_Other:
			if strings.Contains(c.Other.GetName(), "_tool_call") {
				t.Errorf("%s shows an MCP call as the provider's wrapper %q", who, c.Other.GetName())
			}
			if strings.HasSuffix(c.Other.GetName(), mcpTool) {
				named++
			}
			if strings.HasPrefix(strings.TrimSpace(c.Other.GetText()), "{") {
				t.Errorf("%s shows a call's arguments as JSON: %q", who, c.Other.GetText())
			}
		}
	}
	if commands == 0 || named == 0 {
		t.Errorf("%s: %d commands and %d calls of %s among %d tool calls, want some of each", who, commands, named, mcpTool, len(calls))
	}
}
