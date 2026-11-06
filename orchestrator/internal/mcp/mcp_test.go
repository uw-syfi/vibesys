package mcp_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/mcp"
)

// fakeDaemon records what the tools send and answers as scripted.
type fakeDaemon struct {
	sent    []*pb.Command
	receipt *pb.Receipt
	err     error
	agents  []*pb.Agent
}

func (f *fakeDaemon) Dispatch(_ context.Context, cmd *pb.Command) (*pb.Receipt, error) {
	f.sent = append(f.sent, cmd)
	if f.err != nil {
		return nil, f.err
	}
	if f.receipt != nil {
		return f.receipt, nil
	}
	return &pb.Receipt{CommandId: cmd.GetId(), AgentId: "a-2", MessageId: "m-1", Delivery: pb.Delivery_DELIVERY_NEW_TURN}, nil
}

func (f *fakeDaemon) Agents(context.Context) ([]*pb.Agent, error) { return f.agents, nil }

var roles = []core.AgentRole{core.RoleLead, core.RolePlanner, core.RoleWorker, core.RoleJudge, core.RoleCombiner, core.RoleProfiler}

var toolActions = map[string]core.Action{
	"spawn_agent": core.ActionSpawnAgent, "send_message": core.ActionSendMessage, "list_agents": core.ActionListAgents,
	"stop_agent": core.ActionStopAgent, "post_handoff": core.ActionPostHandoff, "post_report": core.ActionPostReport,
}

func newServer(role core.AgentRole, d mcp.Daemon) *mcp.Server {
	return mcp.New(mcp.Identity{Agent: "a-1", Run: "r-1", Role: role}, d, "proc1")
}

func rpc(t testing.TB, s *mcp.Server, id int, method string, params any) map[string]any {
	t.Helper()
	raw, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params})
	out := s.Handle(context.Background(), raw)
	var m map[string]any
	if err := json.Unmarshal(out, &m); err != nil {
		t.Fatalf("answer %q is not JSON: %v", out, err)
	}
	return m
}

func listed(t testing.TB, s *mcp.Server) []string {
	t.Helper()
	var names []string
	for _, tool := range rpc(t, s, 1, "tools/list", map[string]any{})["result"].(map[string]any)["tools"].([]any) {
		names = append(names, tool.(map[string]any)["name"].(string))
	}
	return names
}

func call(t testing.TB, s *mcp.Server, id int, name string, args map[string]any) (text string, isError bool) {
	t.Helper()
	res := rpc(t, s, id, "tools/call", map[string]any{"name": name, "arguments": args})["result"].(map[string]any)
	text = res["content"].([]any)[0].(map[string]any)["text"].(string)
	isError, _ = res["isError"].(bool)
	return text, isError
}

// TestEachRoleSeesExactlyTheToolsTheRoleTableAllows holds the tool list to core/roles.go for every
// role, so opening a tool to a role is one edit there.
func TestEachRoleSeesExactlyTheToolsTheRoleTableAllows(t *testing.T) {
	for _, role := range roles {
		got := map[string]bool{}
		for _, name := range listed(t, newServer(role, &fakeDaemon{})) {
			got[name] = true
		}
		for name, action := range toolActions {
			if want := core.Allowed(core.Principal(role), action); got[name] != want {
				t.Errorf("role %s: tool %s listed=%v, the role table says %v", role, name, got[name], want)
			}
		}
	}
}

func TestACallerCannotNameItselfOrARunInAToolArgument(t *testing.T) {
	d := &fakeDaemon{}
	s := newServer(core.RoleLead, d)
	for _, key := range []string{"run_id", "agent", "caller", "from", "role_of_caller"} {
		text, isErr := call(t, s, 2, "spawn_agent", map[string]any{"role": "worker", "brief": "b", key: "r-evil"})
		if !isErr || !strings.Contains(text, "unknown field") {
			t.Errorf("an argument %q was accepted: %q", key, text)
		}
	}
	if len(d.sent) != 0 {
		t.Fatalf("%d commands were sent from refused calls", len(d.sent))
	}
	if _, isErr := call(t, s, 3, "spawn_agent", map[string]any{"role": "worker", "brief": "b"}); isErr {
		t.Fatal("a valid spawn failed")
	}
	if got := d.sent[0].GetSpawnAgent().GetRunId(); got != "r-1" {
		t.Fatalf("the spawn went to run %q, want the session's r-1", got)
	}
}

func TestAToolOfAnotherRoleIsRefusedWithWhatToUseInstead(t *testing.T) {
	text, isErr := call(t, newServer(core.RoleWorker, &fakeDaemon{}), 1, "spawn_agent", map[string]any{"role": "worker", "brief": "b"})
	if !isErr || !strings.Contains(text, "post_handoff") {
		t.Fatalf("got %q, want a refusal that names the tool a worker has", text)
	}
}

// Every refusal of the contract has advice of its own: none falls to the line that only names it.
func TestEveryRefusalSaysWhatToDoNext(t *testing.T) {
	values := pb.Refusal(0).Descriptor().Values()
	for i := 1; i < values.Len(); i++ {
		r := pb.Refusal(values.Get(i).Number())
		d := &fakeDaemon{receipt: &pb.Receipt{Refusal: r}}
		text, isErr := call(t, newServer(core.RoleWorker, d), 1, "post_handoff", map[string]any{
			"claim": "c", "worker": map[string]any{"no_candidate": map[string]any{"blocker": "b"}},
		})
		if !isErr || !strings.Contains(text, ":") || strings.Contains(text, "REFUSAL_") || strings.Contains(text, "refused the command") || strings.Contains(text, "<no value>") {
			t.Errorf("refusal %v read %q, want advice in words", r, text)
		}
	}
}

// A spawn outside the run's allowance is refused with the list the lead can choose from, in the names
// spawn_agent takes.
func TestARefusedSpawnListsWhatTheOperatorAllowed(t *testing.T) {
	d := &fakeDaemon{receipt: &pb.Receipt{Refusal: pb.Refusal_REFUSAL_AGENT_NOT_ALLOWED, AllowedAgents: []*pb.AllowedAgent{
		{Harness: pb.Harness_HARNESS_CODEX, Model: "gpt-m"}, {Harness: pb.Harness_HARNESS_CLAUDE_CODE},
	}}}
	text, isErr := call(t, newServer(core.RoleLead, d), 1, "spawn_agent", map[string]any{"role": "judge", "brief": "b", "harness": "claude_code", "model": "opus"})
	if !isErr {
		t.Fatalf("got %q, want a refusal", text)
	}
	for _, want := range []string{"harness codex with model gpt-m", "harness claude_code with its default model"} {
		if !strings.Contains(text, want) {
			t.Errorf("the refusal %q does not list %q", text, want)
		}
	}
}

func TestAnUnreachableDaemonSaysToCheckBeforeRepeating(t *testing.T) {
	d := &fakeDaemon{err: status.Error(codes.Unavailable, "socket closed")}
	text, isErr := call(t, newServer(core.RoleLead, d), 1, "send_message", map[string]any{"agent_id": "a-2", "text": "hi"})
	if !isErr || !strings.Contains(text, "list_agents") {
		t.Fatalf("got %q, want it to say to check with list_agents", text)
	}
}

func TestCommandIDsAreStablePerRequestAndDifferAcrossProcesses(t *testing.T) {
	a, b := &fakeDaemon{}, &fakeDaemon{}
	one := mcp.New(mcp.Identity{Agent: "a-1", Run: "r-1", Role: core.RoleLead}, a, "p1")
	two := mcp.New(mcp.Identity{Agent: "a-1", Run: "r-1", Role: core.RoleLead}, b, "p2")
	args := map[string]any{"agent_id": "a-2", "text": "hi"}
	call(t, one, 7, "send_message", args)
	call(t, one, 7, "send_message", args)
	call(t, one, 8, "send_message", args)
	call(t, two, 7, "send_message", args)
	ids := []string{a.sent[0].GetId(), a.sent[1].GetId(), a.sent[2].GetId(), b.sent[0].GetId()}
	if ids[0] != ids[1] || ids[0] == ids[2] || ids[0] == ids[3] {
		t.Fatalf("ids %v: a repeated request must reuse its id, and another request or process must not", ids)
	}
}

func TestListAgentsShowsOnlyTheCallersRun(t *testing.T) {
	d := &fakeDaemon{agents: []*pb.Agent{
		{Id: "a-1", RunId: "r-1", Role: pb.AgentRole_AGENT_ROLE_LEAD, Status: pb.AgentStatus_AGENT_STATUS_IDLE},
		{Id: "z-9", RunId: "r-2", Role: pb.AgentRole_AGENT_ROLE_WORKER},
	}}
	text, isErr := call(t, newServer(core.RoleLead, d), 1, "list_agents", nil)
	if isErr || !strings.Contains(text, "a-1") || strings.Contains(text, "z-9") {
		t.Fatalf("got %q", text)
	}
}

// TestEveryToolSchemaIsValidJSONSchemaForEveryRole holds what a client checks before it offers a
// server's tools: an object schema, no null anywhere (Claude Code drops all of a server's tools
// when one schema has a null), and required naming only listed properties.
func TestEveryToolSchemaIsValidJSONSchemaForEveryRole(t *testing.T) {
	for _, role := range roles {
		tools := rpc(t, newServer(role, &fakeDaemon{}), 1, "tools/list", map[string]any{})["result"].(map[string]any)["tools"].([]any)
		for _, raw := range tools {
			tool := raw.(map[string]any)
			schema := tool["inputSchema"].(map[string]any)
			if path := nullIn(schema, tool["name"].(string)); path != "" {
				t.Errorf("role %s: %s is null", role, path)
			}
			if schema["type"] != "object" {
				t.Errorf("role %s: %s is not an object schema", role, tool["name"])
			}
			props, _ := schema["properties"].(map[string]any)
			required, ok := schema["required"].([]any)
			if !ok {
				t.Errorf("role %s: %s required is %T, not a list", role, tool["name"], schema["required"])
			}
			for _, name := range required {
				if _, ok := props[name.(string)]; !ok {
					t.Errorf("role %s: %s requires %v, which it does not list", role, tool["name"], name)
				}
			}
		}
	}
}

func nullIn(v any, path string) string {
	switch v := v.(type) {
	case nil:
		return path
	case map[string]any:
		for k, child := range v {
			if p := nullIn(child, path+"."+k); p != "" {
				return p
			}
		}
	case []any:
		for i, child := range v {
			if p := nullIn(child, fmt.Sprintf("%s[%d]", path, i)); p != "" {
				return p
			}
		}
	}
	return ""
}

func TestInitializeAndNotificationsFollowTheProtocol(t *testing.T) {
	s := newServer(core.RoleLead, &fakeDaemon{})
	res := rpc(t, s, 1, "initialize", map[string]any{"protocolVersion": "2025-03-26"})["result"].(map[string]any)
	if res["protocolVersion"] != "2025-03-26" {
		t.Fatalf("initialize answered %v", res)
	}
	if out := s.Handle(context.Background(), []byte(`{"jsonrpc":"2.0","method":"notifications/initialized"}`)); out != nil {
		t.Fatalf("a notification was answered with %q", out)
	}
	if e := rpc(t, s, 2, "no/such", nil)["error"].(map[string]any); e["code"].(float64) != -32601 {
		t.Fatalf("unknown method answered %v", e)
	}
}

func TestServeAnswersEachLine(t *testing.T) {
	s := newServer(core.RoleLead, &fakeDaemon{})
	in := strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"ping"}` + "\n\n" + `{"jsonrpc":"2.0","id":2,"method":"ping"}`)
	var out bytes.Buffer
	if err := s.Serve(context.Background(), in, &out); err != nil {
		t.Fatal(err)
	}
	if lines := strings.Split(strings.TrimSpace(out.String()), "\n"); len(lines) != 2 {
		t.Fatalf("got %d answers: %q", len(lines), out.String())
	}
}

func FuzzHandle(f *testing.F) {
	f.Add([]byte(`{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"spawn_agent","arguments":{"role":"worker","brief":"x"}}}`))
	f.Add([]byte(`{"jsonrpc":"2.0","id":"a","method":"tools/list"}`))
	f.Add([]byte(`[{"jsonrpc":"2.0"}]`))
	f.Add([]byte(`{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"post_handoff","arguments":{"claim":5}}}`))
	f.Fuzz(func(t *testing.T, data []byte) {
		for _, role := range []core.AgentRole{core.RoleLead, core.RoleWorker} {
			out := newServer(role, &fakeDaemon{}).Handle(context.Background(), data)
			if out == nil {
				continue
			}
			var m map[string]any
			if err := json.Unmarshal(out, &m); err != nil || m["jsonrpc"] != "2.0" {
				t.Fatalf("input %q answered %q: %v", data, out, fmt.Sprint(err))
			}
		}
	})
}
