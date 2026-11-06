package mcp

import (
	"context"
	"encoding/json"
	"fmt"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

var roleNames = map[string]pb.AgentRole{
	"planner": pb.AgentRole_AGENT_ROLE_PLANNER, "worker": pb.AgentRole_AGENT_ROLE_WORKER,
	"judge": pb.AgentRole_AGENT_ROLE_JUDGE, "combiner": pb.AgentRole_AGENT_ROLE_COMBINER,
	"profiler": pb.AgentRole_AGENT_ROLE_PROFILER,
}

var reportKindNames = map[string]pb.ReportKind{"progress": pb.ReportKind_REPORT_KIND_PROGRESS, "final": pb.ReportKind_REPORT_KIND_FINAL}

var harnessNames = map[string]pb.Harness{"claude_code": pb.Harness_HARNESS_CLAUDE_CODE, "codex": pb.Harness_HARNESS_CODEX}

func harnessName(h pb.Harness) string {
	for name, v := range harnessNames {
		if v == h {
			return name
		}
	}
	return h.String()
}

func str(desc string) map[string]any { return map[string]any{"type": "string", "description": desc} }

// object is a JSON Schema object. A tool with no required argument says so with an empty list:
// JSON Schema has no null there, and Claude Code drops every tool of a server whose list has one.
func object(required []string, props map[string]any) map[string]any {
	if required == nil {
		required = []string{}
	}
	return map[string]any{"type": "object", "properties": props, "required": required, "additionalProperties": false}
}

var allTools = []tool{
	{
		name: "spawn_agent", action: core.ActionSpawnAgent,
		description: "Start a subagent in your run, with a brief for its first turn. It reports back with post_handoff. An empty harness takes yours. An empty model takes the first model the operator allowed on that harness; a harness and model the operator did not allow are refused, with the list of what is allowed. An empty effort takes yours.",
		schema: object([]string{"role", "brief"}, map[string]any{
			"role":        map[string]any{"type": "string", "enum": []string{"planner", "worker", "judge", "combiner", "profiler"}, "description": "What the subagent is for."},
			"brief":       str("Its first prompt."),
			"harness":     map[string]any{"type": "string", "enum": []string{"claude_code", "codex"}, "description": "Optional. The agent CLI to run on."},
			"model":       str("Optional model name."),
			"effort":      str("Optional reasoning effort."),
			"plan_number": map[string]any{"type": "integer", "minimum": 0, "description": "Optional. The plan this agent works on."},
		}),
		run: func(ctx context.Context, s *Server, args json.RawMessage, id string) (string, error) {
			var a struct {
				Role, Brief, Harness, Model, Effort string
				PlanNumber                          uint32 `json:"plan_number"`
			}
			if err := decode(args, &a); err != nil {
				return "", err
			}
			role, ok := roleNames[a.Role]
			if !ok {
				return "", fmt.Errorf("role %q is not one of planner, worker, judge, combiner, profiler: call again with one of them", a.Role)
			}
			spec := &pb.AgentSpec{Model: a.Model, ReasoningEffort: a.Effort}
			if a.Harness != "" {
				h, ok := harnessNames[a.Harness]
				if !ok {
					return "", fmt.Errorf("harness %q is not claude_code or codex: call again with one of them, or leave it out", a.Harness)
				}
				spec.Harness = h
			}
			return s.send(ctx, &pb.Command{Id: id, Body: &pb.Command_SpawnAgent{SpawnAgent: &pb.SpawnAgent{
				RunId: string(s.id.Run), Role: role, Spec: spec, PlanNumber: a.PlanNumber, Brief: a.Brief,
			}}}, func(r *pb.Receipt) string {
				return fmt.Sprintf("started agent %s. It will post a handoff when it is done; list_agents shows where it stands.", r.GetAgentId())
			})
		},
	},
	{
		name: "send_message", action: core.ActionSendMessage,
		description: "Send text to one agent of your run. It reaches the agent as steering if it is working, or starts its next turn if it is idle.",
		schema:      object([]string{"agent_id", "text"}, map[string]any{"agent_id": str("The agent to tell."), "text": str("What to say.")}),
		run: func(ctx context.Context, s *Server, args json.RawMessage, id string) (string, error) {
			var a struct {
				AgentID string `json:"agent_id"`
				Text    string
			}
			if err := decode(args, &a); err != nil {
				return "", err
			}
			return s.send(ctx, &pb.Command{Id: id, Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: a.AgentID, Text: a.Text}}},
				func(r *pb.Receipt) string {
					return fmt.Sprintf("message %s is %s.", r.GetMessageId(), deliveryName(r.GetDelivery()))
				})
		},
	},
	{
		name: "list_agents", action: core.ActionListAgents,
		description: "List the agents of your run with their role, status and plan, so you can see who is working and who is done.",
		schema:      object(nil, map[string]any{}),
		run: func(ctx context.Context, s *Server, args json.RawMessage, _ string) (string, error) {
			var none struct{}
			if err := decode(args, &none); err != nil {
				return "", err
			}
			agents, err := s.d.Agents(ctx)
			if err != nil {
				return "", transportAdvice(err)
			}
			type row struct {
				ID, Role, Status, Parent string
				PlanNumber               uint32 `json:",omitempty"`
			}
			rows := []row{}
			for _, a := range agents {
				if core.RunID(a.GetRunId()) != s.id.Run {
					continue
				}
				rows = append(rows, row{ID: a.GetId(), Role: roleName(a.GetRole()), Status: a.GetStatus().String(), Parent: a.GetParentAgentId(), PlanNumber: a.GetPlanNumber()})
			}
			out, _ := json.MarshalIndent(rows, "", "  ")
			return string(out), nil
		},
	},
	{
		name: "stop_agent", action: core.ActionStopAgent,
		description: "Stop one subagent of your run. It is asked to finish its turn, then its session closes.",
		schema:      object([]string{"agent_id"}, map[string]any{"agent_id": str("The subagent to stop.")}),
		run: func(ctx context.Context, s *Server, args json.RawMessage, id string) (string, error) {
			var a struct {
				AgentID string `json:"agent_id"`
			}
			if err := decode(args, &a); err != nil {
				return "", err
			}
			return s.send(ctx, &pb.Command{Id: id, Body: &pb.Command_StopAgent{StopAgent: &pb.StopAgent{AgentId: a.AgentID}}},
				func(r *pb.Receipt) string { return "agent " + r.GetAgentId() + " is being stopped." })
		},
	},
	{
		name: "post_handoff", action: core.ActionPostHandoff,
		description: "Tell your lead what you did and finish. Call it once, last: your session closes when your turn ends and no mail waits for you.",
		schema: object([]string{"claim"}, map[string]any{
			"plan_number":   map[string]any{"type": "integer", "minimum": 0, "description": "The plan you worked on."},
			"has_candidate": map[string]any{"type": "boolean", "description": "True if you produced a commit worth measuring."},
			"claim":         str("What you did, in one or two sentences."),
			"commit":        str("The commit you produced."),
			"parent_commit": str("The commit it builds on."),
			"branch":        str("The branch that holds it."),
			"checks":        str("Which checks you ran and how they ended."),
			"measurement":   str("What you measured."),
			"learned":       str("What the lead should know."),
			"next":          str("What you would do next."),
			"open_items":    str("What is left undone."),
		}),
		run: func(ctx context.Context, s *Server, args json.RawMessage, id string) (string, error) {
			var a struct {
				PlanNumber                                 uint32 `json:"plan_number"`
				HasCandidate                               bool   `json:"has_candidate"`
				Claim, Commit                              string
				ParentCommit                               string `json:"parent_commit"`
				Branch, Checks, Measurement, Learned, Next string
				OpenItems                                  string `json:"open_items"`
			}
			if err := decode(args, &a); err != nil {
				return "", err
			}
			h := &pb.Handoff{
				PlanNumber: a.PlanNumber, HasCandidate: a.HasCandidate, Claim: a.Claim, Commit: a.Commit,
				ParentCommit: a.ParentCommit, Branch: a.Branch, Checks: a.Checks, Measurement: a.Measurement,
				Learned: a.Learned, Next: a.Next, OpenItems: a.OpenItems,
			}
			return s.send(ctx, &pb.Command{Id: id, Body: &pb.Command_PostHandoff{PostHandoff: &pb.PostHandoff{Handoff: h}}},
				func(*pb.Receipt) string { return "handoff sent to your lead. You are done: end your turn." })
		},
	},
	{
		name: "post_report", action: core.ActionPostReport,
		description: "Send the operator a report. Use kind progress for a note along the way and kind final once, when you have finished what you were asked to do or cannot go on.",
		schema: object([]string{"kind", "headline"}, map[string]any{
			"kind":          map[string]any{"type": "string", "enum": []string{"progress", "final"}, "description": "progress for a note along the way; final for your last word."},
			"headline":      str("One line."),
			"changed":       str("What changed since the last report."),
			"in_flight":     str("What is running now."),
			"decisions":     str("What you decided on your own."),
			"open_for_user": str("What needs the operator."),
		}),
		run: func(ctx context.Context, s *Server, args json.RawMessage, id string) (string, error) {
			var a struct {
				Kind, Headline, Changed, Decisions string
				InFlight                           string `json:"in_flight"`
				OpenForUser                        string `json:"open_for_user"`
			}
			if err := decode(args, &a); err != nil {
				return "", err
			}
			kind, ok := reportKindNames[a.Kind]
			if !ok {
				return "", fmt.Errorf("kind %q is not progress or final: call again with one of them", a.Kind)
			}
			r := &pb.Report{Kind: kind, Headline: a.Headline, Changed: a.Changed, InFlight: a.InFlight, Decisions: a.Decisions, OpenForUser: a.OpenForUser}
			return s.send(ctx, &pb.Command{Id: id, Body: &pb.Command_PostReport{PostReport: &pb.PostReport{Report: r}}},
				func(*pb.Receipt) string { return "report sent to the operator." })
		},
	},
}

func roleName(r pb.AgentRole) string {
	for name, v := range roleNames {
		if v == r {
			return name
		}
	}
	return "lead"
}

func deliveryName(d pb.Delivery) string {
	switch d {
	case pb.Delivery_DELIVERY_STEERED:
		return "steered into the agent's running turn"
	case pb.Delivery_DELIVERY_NEW_TURN:
		return "starting the agent's next turn"
	case pb.Delivery_DELIVERY_QUEUED:
		return "queued until the agent's turn ends"
	case pb.Delivery_DELIVERY_REJECTED:
		return "rejected"
	}
	return "on its way"
}
