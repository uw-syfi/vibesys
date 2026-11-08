package clientcli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"strconv"
	"strings"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli/attachview"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/names"
)

// confinementNotice is printed on every start, to stderr so that --json stays clean.
const confinementNotice = "note: agents run as your user with their permissions bypassed and no confinement."

// noLimitNotice is printed on a start that gives the run neither limit, to stderr so that --json stays
// clean. A run has no default limit, so the person is told it has none and how to give it one.
const noLimitNotice = "note: this run has no time or spending limit; --max-time 30m and --max-cost 5 (dollars) set one."

// startArgs are the flags of `run start`.
type startArgs struct {
	repository, task, brief, harness, model, effort string
	fanout                                          uint32
	detach, json                                    bool
	// maxTime and maxCost are the run's limits, zero for none.
	maxTime time.Duration
	maxCost float64
	// summaryEvery is how often the lead is asked for a progress report, zero for the host's default.
	summaryEvery time.Duration
	// allow is the --allow-agent values, each harness or harness:model.
	allow []string
}

func parseStart(args []string) (startArgs, error) {
	a := startArgs{fanout: 4}
	fs := flags("start")
	fs.StringVar(&a.repository, "repository", "", "the repository the run works in (default: the git root of the current directory)")
	fs.StringVar(&a.task, "task", "", "the task to optimize")
	fs.StringVar(&a.brief, "brief", "", "steering text for the lead")
	fs.StringVar(&a.harness, "harness", names.Spell(pb.Harness_HARNESS_CLAUDE_CODE), names.List[pb.Harness]())
	fs.StringVar(&a.model, "model", "", "the lead's model")
	fs.StringVar(&a.effort, "effort", "", "the lead's reasoning effort")
	fs.Func("allow-agent", "an agent the lead may also use, as harness or harness:model (repeatable)", func(v string) error {
		a.allow = append(a.allow, v)
		return nil
	})
	fs.Func("max-time", "time limit of the run, such as 30m (default: none)", func(v string) error {
		d, err := time.ParseDuration(v)
		if err != nil || d <= 0 {
			return fmt.Errorf("%q is not a positive duration such as 30m", v)
		}
		a.maxTime = d
		return nil
	})
	fs.Func("max-cost", "cost limit of the run in dollars, such as 5 (default: none)", func(v string) error {
		c, err := strconv.ParseFloat(v, 64)
		if err != nil || !(c > 0) || math.IsInf(c, 0) {
			return fmt.Errorf("%q is not a positive number of dollars such as 5", v)
		}
		a.maxCost = c
		return nil
	})
	fs.Func("summary-every", "how often the lead is asked for a progress report while work moves, such as 30m (default: 1h; at least 5m)", func(v string) error {
		d, err := time.ParseDuration(v)
		if err != nil || d <= 0 {
			return fmt.Errorf("%q is not a positive duration such as 30m", v)
		}
		a.summaryEvery = d
		return nil
	})
	fs.Func("fanout", "how many subagents may be live at once (default 4)", func(v string) error {
		n, err := strconv.ParseUint(v, 10, 32)
		if err != nil {
			return fmt.Errorf("%q is not a number of agents", v)
		}
		a.fanout = uint32(n)
		return nil
	})
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
	harness, ok := names.Parse[pb.Harness](a.harness)
	if !ok {
		return nil, usageError("--harness %q is not %s", a.harness, names.List[pb.Harness]())
	}
	start := &pb.StartRun{
		Repository: repo, Task: a.task, FanoutLimit: a.fanout, Brief: a.brief,
		Lead: &pb.AgentSpec{Harness: harness, Model: a.model, ReasoningEffort: a.effort},
	}
	for _, spec := range a.allow {
		choice, err := parseAllowed(spec)
		if err != nil {
			return nil, err
		}
		start.AllowAgents = append(start.AllowAgents, choice)
	}
	if a.maxTime > 0 {
		start.MaxTime = durationpb.New(a.maxTime)
	}
	if a.maxCost > 0 {
		cost := a.maxCost
		start.MaxCostUsd = &cost
	}
	if a.summaryEvery > 0 {
		start.SummaryEvery = durationpb.New(a.summaryEvery)
	}
	return &pb.Command{Id: id, Body: &pb.Command_StartRun{StartRun: start}}, nil
}

// parseAllowed reads one --allow-agent value: a harness, or a harness and a model after the first colon.
// A harness alone allows that harness on its default model only.
func parseAllowed(spec string) (*pb.AllowedAgent, error) {
	name, model, _ := strings.Cut(spec, ":")
	harness, ok := names.Parse[pb.Harness](name)
	if !ok {
		return nil, usageError("--allow-agent %q does not start with %s", spec, names.List[pb.Harness]())
	}
	return &pb.AllowedAgent{Harness: harness, Model: model}, nil
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
	if a.maxTime == 0 && a.maxCost == 0 {
		fmt.Fprintln(c.Stderr, noLimitNotice)
	}
	conn, err := c.openFor(ctx, VerbStart)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(ctx)
	s := &session{client: conn, cancel: cancel}
	defer s.Close()
	receipt, err := within(ctx, c.Clock, serviceBound, "answer", func(ctx context.Context) (*pb.Receipt, error) {
		return conn.Dispatch(ctx, cmd)
	})
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
	return c.watchRun(ctx, s, "", attachview.DefaultHistory)
}

// followUntilLive reads the stream until the run's lead is live. A run that ends first is an
// error that carries the lead's own account of why.
func (c *call) followUntilLive(ctx context.Context, s *session, run string) ([]Change, error) {
	f, snapshot, err := openFeed(ctx, c.Clock, s.client)
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
		return nil, c.runFailed(s.view)
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
	switch r.GetRefusal() {
	case pb.Refusal_REFUSAL_COST_LIMIT_REACHED:
		return failed("refused: run %s has reached its cost limit and is finishing, so that limit stays as it is", r.GetRunId())
	case pb.Refusal_REFUSAL_TIME_LIMIT_REACHED:
		return failed("refused: run %s has reached its time limit and is finishing, so that limit stays as it is", r.GetRunId())
	case pb.Refusal_REFUSAL_MODEL_NOT_ALLOWED:
		return failed("refused: the host does not allow %s; it allows %s", textOr(r.GetModel(), "a model of that tool"), textOr(strings.Join(r.GetAllowedModels(), ", "), "none"))
	}
	return failed("refused: %s", statusWord(r.GetRefusal().String(), "REFUSAL_"))
}

// textOr is text, or otherwise when text is empty.
func textOr(text, otherwise string) string {
	if text == "" {
		return otherwise
	}
	return text
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
