package mcp

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/names"
)

// tool is one tool. Its name, its description and its arguments come from the proto message that
// is its input (see tool_options.proto); only the action that decides who sees it and what it does
// with the arguments are written here.
type tool struct {
	name, description string
	action            core.Action
	input             protoreflect.MessageDescriptor
	schema            map[string]any
	// read fills a new input from the arguments of a call and holds it to its proto's rules.
	// ownership, when a tool has it, is checked after the arguments are read and before the rules
	// are: it speaks to the caller with the words that fit its role.
	read func(s *Server, args json.RawMessage) (proto.Message, error)
	run  func(ctx context.Context, s *Server, in proto.Message, commandID string) (string, error)
}

// define builds a tool from the message M that is its input.
func define[M proto.Message](action core.Action, newInput func() M, ownership func(*Server, M) error, run func(context.Context, *Server, M, string) (string, error)) tool {
	md := newInput().ProtoReflect().Descriptor()
	opt, _ := proto.GetExtension(md.Options(), pb.E_McpTool).(*pb.McpTool)
	if opt.GetName() == "" || opt.GetDescription() == "" {
		panic(fmt.Sprintf("mcp: message %s is a tool's input and carries no mcp_tool name and description", md.FullName()))
	}
	return tool{
		name: opt.GetName(), description: opt.GetDescription(), action: action, input: md, schema: inputSchema(md),
		read: func(s *Server, args json.RawMessage) (proto.Message, error) {
			in := newInput()
			if err := readInput(opt.GetName(), args, in); err != nil {
				return nil, err
			}
			if ownership != nil {
				if err := ownership(s, in); err != nil {
					return nil, err
				}
			}
			if err := checkInput(opt.GetName(), in); err != nil {
				return nil, err
			}
			return in, nil
		},
		run: func(ctx context.Context, s *Server, in proto.Message, id string) (string, error) {
			return run(ctx, s, in.(M), id)
		},
	}
}

var allTools = []tool{
	define(core.ActionSpawnAgent, func() *pb.SpawnAgentInput { return &pb.SpawnAgentInput{} }, nil,
		func(ctx context.Context, s *Server, in *pb.SpawnAgentInput, id string) (string, error) {
			spec := &pb.AgentSpec{Harness: in.GetHarness(), Model: in.GetModel(), ReasoningEffort: in.GetEffort()}
			return s.send(ctx, &pb.Command{Id: id, Body: &pb.Command_SpawnAgent{SpawnAgent: &pb.SpawnAgent{
				RunId: string(s.id.Run), Role: in.GetRole(), Spec: spec, PlanNumber: in.GetPlanNumber(), Brief: in.GetBrief(),
			}}}, func(r *pb.Receipt) string { return say("spawn_agent", struct{ Agent string }{r.GetAgentId()}) })
		}),
	define(core.ActionSendMessage, func() *pb.SendMessage { return &pb.SendMessage{} }, nil,
		func(ctx context.Context, s *Server, in *pb.SendMessage, id string) (string, error) {
			return s.send(ctx, &pb.Command{Id: id, Body: &pb.Command_SendMessage{SendMessage: in}}, func(r *pb.Receipt) string {
				return say("send_message", struct{ Message, Delivery string }{r.GetMessageId(), names.Spell(r.GetDelivery())})
			})
		}),
	define(core.ActionListAgents, func() *pb.ListAgentsInput { return &pb.ListAgentsInput{} }, nil,
		func(ctx context.Context, s *Server, _ *pb.ListAgentsInput, _ string) (string, error) {
			agents, err := s.d.Agents(ctx)
			if err != nil {
				return "", transportAdvice(err)
			}
			type row struct {
				ID, Role, Status, Parent string
				Plan                     uint32
				Self                     bool
			}
			rows := []row{}
			for _, a := range agents {
				if core.RunID(a.GetRunId()) != s.id.Run {
					continue
				}
				rows = append(rows, row{
					ID: a.GetId(), Role: names.Spell(a.GetRole()), Status: names.Spell(a.GetStatus()), Parent: a.GetParentAgentId(),
					Plan: a.GetPlanNumber(), Self: core.AgentID(a.GetId()) == s.id.Agent,
				})
			}
			return say("list_agents", struct{ Agents []row }{rows}), nil
		}),
	define(core.ActionStopAgent, func() *pb.StopAgent { return &pb.StopAgent{} }, nil,
		func(ctx context.Context, s *Server, in *pb.StopAgent, id string) (string, error) {
			return s.send(ctx, &pb.Command{Id: id, Body: &pb.Command_StopAgent{StopAgent: in}},
				func(r *pb.Receipt) string { return say("stop_agent", struct{ Agent string }{r.GetAgentId()}) })
		}),
	define(core.ActionPostHandoff, func() *pb.Handoff { return &pb.Handoff{} }, ownBody,
		func(ctx context.Context, s *Server, in *pb.Handoff, id string) (string, error) {
			return s.send(ctx, &pb.Command{Id: id, Body: &pb.Command_PostHandoff{PostHandoff: &pb.PostHandoff{Handoff: in}}},
				func(*pb.Receipt) string { return say("post_handoff", nil) })
		}),
	define(core.ActionPostReport, func() *pb.Report { return &pb.Report{} }, nil,
		func(ctx context.Context, s *Server, in *pb.Report, id string) (string, error) {
			return s.send(ctx, &pb.Command{Id: id, Body: &pb.Command_PostReport{PostReport: &pb.PostReport{Report: in}}},
				func(*pb.Receipt) string { return say("post_report", nil) })
		}),
}

// ownBody holds a handoff to the body of the caller's role. The body is a field named for the
// role, and a handoff with none or with another's is refused saying which to fill.
func ownBody(s *Server, h *pb.Handoff) error {
	own := string(s.id.Role)
	given := h.ProtoReflect().WhichOneof(h.ProtoReflect().Descriptor().Oneofs().ByName("body"))
	switch {
	case given == nil:
		return &answerError{say("missing_role_body", struct{ Role string }{own})}
	case string(given.Name()) != own:
		return &answerError{say("wrong_role_body", struct{ Role, Given string }{own, string(given.Name())})}
	}
	return nil
}

// answerError is a refusal that is already in the words an agent reads.
type answerError struct{ text string }

func (e *answerError) Error() string { return e.text }

// toolFor finds a tool by name.
func toolFor(name string) (tool, bool) {
	i := slices.IndexFunc(allTools, func(t tool) bool { return t.name == name })
	if i < 0 {
		return tool{}, false
	}
	return allTools[i], true
}
