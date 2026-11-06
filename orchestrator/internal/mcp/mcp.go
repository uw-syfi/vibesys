// Package mcp is the tool server an agent uses to reach the orchestrator: a Model Context Protocol
// server over stdio, one per agent session. It exposes the tools the agent's role may use and
// nothing else.
//
// Who is calling comes from the session, set in the environment when the agent was launched, and
// never from a tool's arguments: the arguments of every tool are checked against a fixed list of
// keys, and none of them names a caller or a run. The daemon checks the session's token again, so
// a tool server that lies about its identity is refused there.
//
// Handle is a pure function of the request bytes, the identity and the daemon's answers, so the
// server's parsing is fuzzed and its tools are tested with a Fake daemon.
package mcp

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"slices"
	"strings"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/names"
)

// Daemon is what the tools need of the orchestrator. client.Client implements it for a session.
type Daemon interface {
	Dispatch(ctx context.Context, cmd *pb.Command) (*pb.Receipt, error)
	// Agents lists the agents of the caller's run.
	Agents(ctx context.Context) ([]*pb.Agent, error)
}

// Identity is who the server speaks for, from the environment the session was launched with.
type Identity struct {
	Agent core.AgentID
	Run   core.RunID
	Role  core.AgentRole
}

// Server is the tool server of one session.
type Server struct {
	id   Identity
	d    Daemon
	proc string
}

// New returns the server of a session. process is unique to this process, so that the ids of
// its commands cannot meet those of an earlier server of the same session.
func New(id Identity, d Daemon, process string) *Server { return &Server{id: id, d: d, proc: process} }

// ServerName is the name the tool server goes by: the host registers it under this name, so an
// agent's harness shows the tools as mcp__vibesys__spawn_agent and so on, and the skills name the
// tools through it. The server says the same name of itself.
const ServerName = "vibesys"

const (
	latestProtocol = "2025-06-18"
	jsonrpcVersion = "2.0"
)

var supportedProtocols = []string{"2024-11-05", "2025-03-26", latestProtocol}

type request struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type response struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

// The JSON-RPC error codes this server uses.
const (
	codeInvalidRequest = -32600
	codeNoMethod       = -32601
	codeInvalidParams  = -32602
	codeParse          = -32700
)

// Serve reads one JSON-RPC message per line from r and writes the answers to w, until r ends.
func (s *Server) Serve(ctx context.Context, r io.Reader, w io.Writer) error {
	in := bufio.NewReaderSize(r, 1<<16)
	for {
		line, err := in.ReadBytes('\n')
		if len(bytes.TrimSpace(line)) > 0 {
			if out := s.Handle(ctx, line); out != nil {
				if _, werr := w.Write(append(out, '\n')); werr != nil {
					return werr
				}
			}
		}
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
	}
}

// Handle answers one message. It returns nil for a notification, which has no answer.
func (s *Server) Handle(ctx context.Context, line []byte) []byte {
	var req request
	dec := json.NewDecoder(bytes.NewReader(line))
	if err := dec.Decode(&req); err != nil || req.JSONRPC != jsonrpcVersion {
		return marshal(response{JSONRPC: jsonrpcVersion, ID: json.RawMessage("null"),
			Error: &rpcError{Code: codeParse, Message: "send one JSON-RPC 2.0 object per line"}})
	}
	if dec.More() {
		return marshal(response{JSONRPC: jsonrpcVersion, ID: idOrNull(req.ID),
			Error: &rpcError{Code: codeInvalidRequest, Message: "send one message per line"}})
	}
	if len(req.ID) == 0 { // a notification: nothing to answer, whatever it is.
		return nil
	}
	result, rerr := s.dispatch(ctx, req)
	return marshal(response{JSONRPC: jsonrpcVersion, ID: idOrNull(req.ID), Result: result, Error: rerr})
}

func idOrNull(id json.RawMessage) json.RawMessage {
	if len(id) == 0 {
		return json.RawMessage("null")
	}
	return id
}

func marshal(v any) []byte {
	out, err := json.Marshal(v)
	if err != nil {
		// Every value marshalled here is built from strings and raw JSON that already parsed.
		return []byte(`{"jsonrpc":"2.0","id":null,"error":{"code":-32603,"message":"internal error"}}`)
	}
	return out
}

func (s *Server) dispatch(ctx context.Context, req request) (any, *rpcError) {
	switch req.Method {
	case "initialize":
		var p struct {
			ProtocolVersion string `json:"protocolVersion"`
		}
		_ = json.Unmarshal(req.Params, &p)
		version := latestProtocol
		if slices.Contains(supportedProtocols, p.ProtocolVersion) {
			version = p.ProtocolVersion
		}
		return map[string]any{
			"protocolVersion": version,
			"capabilities":    map[string]any{"tools": map[string]any{}},
			"serverInfo":      map[string]any{"name": ServerName, "version": "1"},
		}, nil
	case "ping":
		return map[string]any{}, nil
	case "tools/list":
		var tools []map[string]any
		for _, t := range s.tools() {
			tools = append(tools, map[string]any{"name": t.name, "description": t.description, "inputSchema": t.schema})
		}
		return map[string]any{"tools": tools}, nil
	case "tools/call":
		var p struct {
			Name      string          `json:"name"`
			Arguments json.RawMessage `json:"arguments"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil || p.Name == "" {
			return nil, &rpcError{Code: codeInvalidParams, Message: "tools/call needs a name and an arguments object"}
		}
		text, err := s.call(ctx, p.Name, p.Arguments, string(req.ID))
		if err != nil {
			return map[string]any{"isError": true, "content": []map[string]any{{"type": "text", "text": err.Error()}}}, nil
		}
		return map[string]any{"content": []map[string]any{{"type": "text", "text": text}}}, nil
	}
	return nil, &rpcError{Code: codeNoMethod, Message: "unknown method " + req.Method}
}

// tools lists what this session's role may use, in a fixed order.
func (s *Server) tools() []tool {
	var out []tool
	for _, t := range allTools {
		if core.Allowed(core.Principal(s.id.Role), t.action) {
			out = append(out, t)
		}
	}
	return out
}

func (s *Server) call(ctx context.Context, name string, args json.RawMessage, requestID string) (string, error) {
	t, ok := toolFor(name)
	if !ok {
		return "", errors.New(say("no_such_tool", struct{ Name, Tools string }{name, s.toolNames()}))
	}
	if !core.Allowed(core.Principal(s.id.Role), t.action) {
		return "", errors.New(say("tool_not_for_role", struct{ Name, Role, Tools string }{name, string(s.id.Role), s.toolNames()}))
	}
	if trimmed := bytes.TrimSpace(args); len(trimmed) == 0 || string(trimmed) == "null" { // a tool that takes nothing may be called with none.
		args = json.RawMessage("{}")
	}
	in, err := t.read(s, args)
	if err != nil {
		return "", err
	}
	commandID := fmt.Sprintf("mcp/%s/%s/%s", s.id.Agent, s.proc, strings.Trim(requestID, `"`))
	return t.run(ctx, s, in, commandID)
}

func (s *Server) toolNames() string {
	var names []string
	for _, t := range s.tools() {
		names = append(names, t.name)
	}
	return strings.Join(names, ", ")
}

// send dispatches a command and turns the receipt, or the failure, into what the agent reads.
func (s *Server) send(ctx context.Context, cmd *pb.Command, done func(*pb.Receipt) string) (string, error) {
	receipt, err := s.d.Dispatch(ctx, cmd)
	if err != nil {
		return "", transportAdvice(err)
	}
	if r := receipt.GetRefusal(); r != pb.Refusal_REFUSAL_UNSPECIFIED {
		return "", errors.New(s.refusalAdvice(receipt))
	}
	return done(receipt), nil
}

func transportAdvice(err error) error {
	data := struct{ Message string }{status.Convert(err).Message()}
	switch status.Code(err) {
	case codes.Unauthenticated, codes.PermissionDenied:
		return errors.New(say("transport_refused", data))
	case codes.InvalidArgument:
		return errors.New(say("transport_rejected", data))
	}
	return errors.New(say("transport_unanswered", data))
}

// refusalAdvice words a refusal for the agent, saying what to do next. The refusal arrives spelled
// as its enum is, and the template that words it chooses by that spelling.
func (s *Server) refusalAdvice(receipt *pb.Receipt) string {
	type agent struct{ Harness, Model string }
	var allowed []agent
	for _, c := range receipt.GetAllowedAgents() {
		allowed = append(allowed, agent{Harness: names.Spell(c.GetHarness()), Model: c.GetModel()})
	}
	return say("refusal", struct {
		Refusal, Role string
		Allowed       []agent
	}{names.Spell(receipt.GetRefusal()), string(s.id.Role), allowed})
}
