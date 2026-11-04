package client

import (
	"context"
	"errors"
	"fmt"
	"io"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// Headless is the command-line client: it turns each verb into calls on a Client and prints what
// comes back, one JSON object per line. It uses the public service API and nothing else, so the
// desktop app can make exactly the same calls.
type Headless struct {
	Client *Client
	IDs    IDs
	Out    io.Writer
}

// StartArgs is what `run start` asks for.
type StartArgs struct {
	Repository, Task, Brief string
	FanoutLimit             uint32
	// Harness is claude_code or codex.
	Harness       string
	Model, Effort string
	// Deadline is RFC 3339, or empty for none.
	Deadline string
}

func (h Headless) print(m proto.Message) error {
	line, err := protojson.MarshalOptions{}.Marshal(m)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(h.Out, "%s\n", line)
	return err
}

// dispatch sends one command and prints its receipt, turning a refusal into an error that says so.
func (h Headless) dispatch(ctx context.Context, cmd *pb.Command) error {
	receipt, err := h.Client.Dispatch(ctx, cmd)
	if err != nil {
		return err
	}
	if err := h.print(receipt); err != nil {
		return err
	}
	if r := receipt.GetRefusal(); r != pb.Refusal_REFUSAL_UNSPECIFIED {
		return fmt.Errorf("the orchestrator refused the command: %s", r)
	}
	return nil
}

// StartRun starts a run and its lead.
func (h Headless) StartRun(ctx context.Context, a StartArgs) error {
	harness, ok := map[string]pb.Harness{"claude_code": pb.Harness_HARNESS_CLAUDE_CODE, "codex": pb.Harness_HARNESS_CODEX}[a.Harness]
	if !ok {
		return fmt.Errorf("harness %q is not claude_code or codex: pass --harness with one of them", a.Harness)
	}
	start := &pb.StartRun{
		Repository: a.Repository, Task: a.Task, FanoutLimit: a.FanoutLimit, Brief: a.Brief,
		Lead: &pb.AgentSpec{Harness: harness, Model: a.Model, ReasoningEffort: a.Effort},
	}
	if a.Deadline != "" {
		at, err := time.Parse(time.RFC3339, a.Deadline)
		if err != nil {
			return fmt.Errorf("deadline %q is not an RFC 3339 time such as 2026-01-02T15:04:05Z: %w", a.Deadline, err)
		}
		start.Deadline = protoTime(at)
	}
	return h.dispatch(ctx, &pb.Command{Id: h.IDs.NewCommandID(), Body: &pb.Command_StartRun{StartRun: start}})
}

// StopRun stops a run and every agent in it.
func (h Headless) StopRun(ctx context.Context, run string) error {
	return h.dispatch(ctx, &pb.Command{Id: h.IDs.NewCommandID(), Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: run}}})
}

// Send sends text to an agent as the operator.
func (h Headless) Send(ctx context.Context, agent, text string) error {
	return h.dispatch(ctx, &pb.Command{Id: h.IDs.NewCommandID(), Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: agent, Text: text}}})
}

// Watch prints a snapshot of runs and then every event after it, until ctx ends or the stream does.
// A stream that ends because ctx did is not an error.
func (h Headless) Watch(ctx context.Context, after uint64) error {
	stream, err := h.Client.WatchRuns(ctx, after)
	if err != nil {
		if ctx.Err() != nil {
			return nil
		}
		return err
	}
	for {
		msg, err := stream.Recv()
		switch {
		case ctx.Err() != nil:
			return nil
		case errors.Is(err, io.EOF):
			return nil
		case err != nil:
			return err
		}
		if err := h.print(msg); err != nil {
			return err
		}
	}
}

func protoTime(t time.Time) *timestamppb.Timestamp { return timestamppb.New(t) }
