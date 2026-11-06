package clientcli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// confinementNotice is printed on every start, to stderr so that --json stays clean.
const confinementNotice = "note: agents run as your user with their permissions bypassed and no confinement."

var harnesses = map[string]pb.Harness{"claude_code": pb.Harness_HARNESS_CLAUDE_CODE, "codex": pb.Harness_HARNESS_CODEX}

// startArgs are the flags of `run start`.
type startArgs struct {
	repository, task, brief, harness, model, effort, deadline string
	fanout                                                    uint
	detach, json                                              bool
}

func parseStart(args []string) (startArgs, error) {
	var a startArgs
	fs := flags("start")
	fs.StringVar(&a.repository, "repository", "", "the repository the run works in (default: the git root of the current directory)")
	fs.StringVar(&a.task, "task", "", "the task to optimize")
	fs.StringVar(&a.brief, "brief", "", "steering text for the lead")
	fs.StringVar(&a.harness, "harness", "claude_code", "claude_code or codex")
	fs.StringVar(&a.model, "model", "", "the lead's model")
	fs.StringVar(&a.effort, "effort", "", "the lead's reasoning effort")
	fs.StringVar(&a.deadline, "deadline", "", "RFC 3339 time the run stops by")
	fs.UintVar(&a.fanout, "fanout", 4, "how many subagents may be live at once")
	fs.BoolVar(&a.detach, "detach", false, "return once the lead is live instead of attaching")
	fs.BoolVar(&a.json, "json", false, "print the receipt as JSON")
	positional, err := parse(fs, args)
	switch {
	case err != nil:
		return a, err
	case len(positional) > 0:
		return a, usageError("run start takes no argument, got %q", positional[0])
	case a.task == "":
		return a, usageError("run start needs --task")
	}
	return a, nil
}

// command builds the StartRun command for repo.
func (a startArgs) command(id, repo string) (*pb.Command, error) {
	harness, ok := harnesses[a.harness]
	if !ok {
		return nil, usageError("--harness %q is not claude_code or codex", a.harness)
	}
	start := &pb.StartRun{
		Repository: repo, Task: a.task, FanoutLimit: uint32(a.fanout), Brief: a.brief,
		Lead: &pb.AgentSpec{Harness: harness, Model: a.model, ReasoningEffort: a.effort},
	}
	if a.deadline != "" {
		at, err := time.Parse(time.RFC3339, a.deadline)
		if err != nil {
			return nil, usageError("--deadline %q is not an RFC 3339 time such as 2026-01-02T15:04:05Z", a.deadline)
		}
		start.Deadline = timestamppb.New(at)
	}
	return &pb.Command{Id: id, Body: &pb.Command_StartRun{StartRun: start}}, nil
}

// start dispatches a run and follows the stream until its lead is live or the run is dead. A
// person at a terminal is then attached to it.
func start(ctx context.Context, c *call, args []string) error {
	a, err := parseStart(args)
	if err != nil {
		return err
	}
	repo, err := c.repository(a.repository)
	if err != nil {
		return err
	}
	cmd, err := a.command(c.IDs.NewCommandID(), repo)
	if err != nil {
		return err
	}
	fmt.Fprintln(c.Stderr, confinementNotice)
	conn, err := c.open(ctx)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(ctx)
	s := &session{client: conn, cancel: cancel}
	defer s.Close()
	receipt, err := conn.Dispatch(ctx, cmd)
	if err != nil {
		return classify(err)
	}
	if err := refusal(receipt); err != nil {
		return err
	}
	if a.json {
		if err := printJSON(c.Stdout, receipt); err != nil {
			return err
		}
	}
	history, err := c.followUntilLive(ctx, s, receipt.GetRunId())
	if err != nil {
		return err
	}
	if !a.json {
		fmt.Fprintf(c.Stdout, "run %s started in %s (task %s); %s is live\n", receipt.GetRunId(), repo, a.task, agentLabel(s.view.Lead(), s.view.Run.GetLeadAgentId()))
	}
	if !c.Terminal || a.detach || a.json {
		return nil
	}
	s.history = history
	return c.watchRun(ctx, s)
}

// followUntilLive reads the stream until the run's lead is live. A run that ends first is an
// error that carries the lead's own account of why.
func (c *call) followUntilLive(ctx context.Context, s *session, run string) ([]Change, error) {
	f, snapshot, err := openFeed(ctx, s.client)
	if err != nil {
		return nil, err
	}
	s.feed, s.view = f, NewView(run)
	history := s.view.Load(snapshot)
	for !s.view.Started() && !s.view.Over() {
		changes, err := f.next(s.view)
		if err != nil {
			if ctx.Err() != nil {
				c.detached(run)
				return nil, failed("interrupted before the lead was live")
			}
			return nil, err
		}
		history = append(history, changes...)
	}
	switch {
	case s.view.Run.GetStatus() == pb.RunStatus_RUN_STATUS_FAILED:
		return nil, failed("run %s failed: %s", run, orUnknown(s.view.failureDetail(s.view.Run)))
	case !s.view.Started():
		return nil, failed("run %s ended before its lead was live", run)
	}
	return history, nil
}

func orUnknown(reason string) string {
	if reason == "" {
		return "no reason was recorded"
	}
	return reason
}

// refusal is the error for a command the service did not carry out, in the service's words.
func refusal(r *pb.Receipt) error {
	if r.GetRefusal() == pb.Refusal_REFUSAL_UNSPECIFIED {
		return nil
	}
	if r.GetRefusal() == pb.Refusal_REFUSAL_REPOSITORY_IN_USE {
		return failed("refused: run %[1]s is already working in this repository, and one repository takes one run at a time.\n"+
			"  to follow it:  vibesys-client run attach %[1]s\n  to end it:     vibesys-client run stop %[1]s", r.GetRunId())
	}
	return failed("refused: %s", statusWord(r.GetRefusal().String(), "REFUSAL_"))
}

// printJSON writes a message as one line of protojson. protojson varies its white space from
// build to build on purpose, so the line is compacted to be the same every time.
func printJSON(w io.Writer, m proto.Message) error {
	raw, err := protojson.Marshal(m)
	if err != nil {
		return err
	}
	var line bytes.Buffer
	if err := json.Compact(&line, raw); err != nil {
		return err
	}
	line.WriteByte('\n')
	_, err = w.Write(line.Bytes())
	return err
}
