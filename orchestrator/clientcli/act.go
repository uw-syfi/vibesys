package clientcli

import (
	"bufio"
	"context"
	"fmt"
	"math"
	"strconv"
	"strings"
	"time"

	"google.golang.org/protobuf/types/known/durationpb"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli/attachview"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/names"
)

// steer sends text to a run's lead, or to the agent named by --agent.
func steer(ctx context.Context, c *call, args []string) error {
	fs := flags("steer")
	agent := fs.String("agent", "", "the agent to send to, by the name attach shows (such as worker-1) or by id (default: the run's lead)")
	asJSON := fs.Bool("json", false, "print the receipt as JSON")
	positional, err := parse(fs, args)
	if err != nil {
		return err
	}
	arg, text := "", ""
	switch len(positional) {
	case 1:
		text = positional[0]
	case 2:
		arg, text = positional[0], positional[1]
	default:
		return usageError("run steer takes [RUN] TEXT, got %d arguments", len(positional))
	}
	s, ctx, err := c.join(ctx, arg)
	if err != nil {
		return err
	}
	defer s.Close()
	if run := s.view.Run; !live(run) {
		return failed("run %s has already %s, so nothing would act on a steer; it was not sent. Start a new run with `run start`", run.GetId(), statusWord(run.GetStatus().String(), "RUN_STATUS_"))
	}
	// --agent takes the name the attach view shows (worker-1) or an id. What names no agent of the run
	// goes to the service as it was typed, and the service refuses an agent it does not know.
	target := *agent
	if target == "" {
		target = s.view.Run.GetLeadAgentId()
	} else if id, err := s.view.ResolveAgent(target); err == nil {
		target = id
	}
	receipt, err := c.dispatch(ctx, s, &pb.Command{Id: c.IDs.NewCommandID(), Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: target, Text: text}}})
	if err != nil {
		return err
	}
	if *asJSON {
		return printJSON(c.Stdout, receipt)
	}
	words, err := c.delivery(ctx, s, receipt, target)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintln(c.Stdout, words)
	return err
}

// dispatch sends a command and turns a refusal into an error.
func (c *call) dispatch(ctx context.Context, s *session, cmd *pb.Command) (*pb.Receipt, error) {
	receipt, err := within(ctx, c.Clock, serviceBound, "answer", func(ctx context.Context) (*pb.Receipt, error) {
		return s.client.Dispatch(ctx, cmd)
	})
	if err != nil {
		return nil, classify(err)
	}
	return receipt, refusal(receipt)
}

// delivery says in words where a steering message stands. The answer is read from the message as the
// stream records it, after every event up to the receipt: the log and this answer are then one
// decision, and the log's line for the same message later says what became of it.
func (c *call) delivery(ctx context.Context, s *session, receipt *pb.Receipt, target string) (string, error) {
	who := agentLabel(s.view.Agents[target], target)
	for s.view.Sequence < receipt.GetSequence() {
		if _, err := s.feed.next(s.view); err != nil {
			return "", err
		}
	}
	delivery := receipt.GetDelivery()
	if recorded, ok := s.view.Messages[receipt.GetMessageId()]; ok {
		delivery = recorded.GetDelivery()
	}
	switch delivery {
	case pb.Delivery_DELIVERY_QUEUED:
		return fmt.Sprintf("queued: %s will see it when its current work ends", who), nil
	case pb.Delivery_DELIVERY_STEERED:
		return fmt.Sprintf("sent into the running turn of %s", who), nil
	case pb.Delivery_DELIVERY_NEW_TURN:
		return fmt.Sprintf("started a new turn for %s", who), nil
	case pb.Delivery_DELIVERY_CONSUMED:
		return fmt.Sprintf("%s has it", who), nil
	case pb.Delivery_DELIVERY_REJECTED:
		reason := statusWord(s.view.Messages[receipt.GetMessageId()].GetRejection().String(), "REJECTION_")
		return "", failed("%s did not take the message: %s", who, reason)
	}
	return fmt.Sprintf("accepted for %s", who), nil
}

// stop ends a run and every agent in it. A person at a terminal is asked first. A run that has already
// ended is what the caller asked for, so that is said, with no error.
func stop(ctx context.Context, c *call, args []string) error {
	fs := flags("stop")
	yes := fs.Bool("yes", false, "do not ask for confirmation")
	positional, err := parse(fs, args)
	if err != nil {
		return err
	}
	if len(positional) > 1 {
		return usageError("run stop takes at most one RUN, got %d arguments", len(positional))
	}
	arg := ""
	if len(positional) == 1 {
		arg = positional[0]
	}
	s, ctx, err := c.join(ctx, arg)
	if err != nil {
		return err
	}
	defer s.Close()
	run := s.view.Run
	if !live(run) {
		_, err = fmt.Fprintf(c.Stdout, "run %s has already %s; there is nothing to stop\n", run.GetId(), statusWord(run.GetStatus().String(), "RUN_STATUS_"))
		return err
	}
	if c.Terminal && !*yes && !c.confirm(run) {
		return failed("run %s was not stopped", run.GetId())
	}
	if _, err := c.dispatch(ctx, s, &pb.Command{Id: c.IDs.NewCommandID(), Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: run.GetId()}}}); err != nil {
		return err
	}
	_, err = fmt.Fprintf(c.Stdout, "stopping run %s; wait for it to end with: %s --until ended --timeout 5m\n", run.GetId(), c.command("wait", run.GetId()))
	return err
}

// confirm asks on the terminal whether to stop the run.
func (c *call) confirm(run *pb.Run) bool {
	fmt.Fprintf(c.Stderr, "Stop run %s (task %s in %s) and all its agents? [y/N] ", run.GetId(), run.GetTask(), run.GetRepository())
	answer, _ := bufio.NewReader(c.Stdin).ReadString('\n')
	answer = strings.ToLower(strings.TrimSpace(answer))
	return answer == "y" || answer == "yes"
}

// updateArgs are the flags of `run update`. Each names a setting to change, and a setting left out stays as
// it is.
type updateArgs struct {
	cost          *pb.CostLimitUpdate
	span          *pb.TimeLimitUpdate
	fanout        *uint32
	harness       *pb.Harness
	model, effort *string
	json          bool
}

// noLimit is what a limit flag says to remove the limit, and what a limit that is not set is called.
const noLimit = "none"

func parseUpdate(args []string) (updateArgs, string, error) {
	var a updateArgs
	fs := flags("update")
	fs.Func("max-cost", "the run's cost limit in dollars, such as 5, or none to remove it", func(v string) error {
		if strings.EqualFold(v, noLimit) {
			a.cost = &pb.CostLimitUpdate{Change: &pb.CostLimitUpdate_Remove{Remove: true}}
			return nil
		}
		c, err := strconv.ParseFloat(v, 64)
		if err != nil || !(c > 0) || math.IsInf(c, 0) {
			return fmt.Errorf("%q is not a positive number of dollars such as 5, or none", v)
		}
		a.cost = &pb.CostLimitUpdate{Change: &pb.CostLimitUpdate_SetUsd{SetUsd: c}}
		return nil
	})
	fs.Func("max-time", "the most time the run may take in all, counted from its start, such as 8h, or none to remove it", func(v string) error {
		if strings.EqualFold(v, noLimit) {
			a.span = &pb.TimeLimitUpdate{Change: &pb.TimeLimitUpdate_Remove{Remove: true}}
			return nil
		}
		d, err := time.ParseDuration(v)
		if err != nil || d <= 0 {
			return fmt.Errorf("%q is not a positive duration such as 8h, or none", v)
		}
		a.span = &pb.TimeLimitUpdate{Change: &pb.TimeLimitUpdate_Set{Set: durationpb.New(d)}}
		return nil
	})
	fs.Func("fanout", "how many subagents may be live at once, at least 1; lowering it stops none", func(v string) error {
		n, err := strconv.ParseUint(v, 10, 32)
		if err != nil || n < 1 {
			return fmt.Errorf("%q is not a number of agents, at least 1", v)
		}
		fanout := uint32(n)
		a.fanout = &fanout
		return nil
	})
	fs.Func("harness", names.List[pb.Harness]()+": the lead's tool, from the end of its current turn", func(v string) error {
		harness, ok := names.Parse[pb.Harness](v)
		if !ok {
			return fmt.Errorf("%q is not %s", v, names.List[pb.Harness]())
		}
		a.harness = &harness
		return nil
	})
	fs.Func("model", "the lead's model: its own when only the model or the effort changes, the tool's default when the tool does", func(v string) error {
		a.model = &v
		return nil
	})
	fs.Func("effort", "the lead's reasoning effort", func(v string) error {
		a.effort = &v
		return nil
	})
	fs.BoolVar(&a.json, "json", false, "print the receipt as JSON")
	positional, err := parse(fs, args)
	switch {
	case err != nil:
		return a, "", err
	case len(positional) > 1:
		return a, "", usageError("run update takes at most one RUN, got %d arguments", len(positional))
	case a.cost == nil && a.span == nil && a.fanout == nil && a.harness == nil && a.model == nil && a.effort == nil:
		return a, "", usageError("run update needs a setting to change: --max-cost, --max-time, --fanout, --harness, --model or --effort")
	}
	arg := ""
	if len(positional) == 1 {
		arg = positional[0]
	}
	return a, arg, nil
}

// leadSpec is the lead the flags ask for, or nil when they ask for none. What a flag leaves out is the
// lead's own, unless the tool changes: another tool has its own default model and effort.
func (a updateArgs) leadSpec(current *pb.Agent) (*pb.AgentSpec, error) {
	if a.harness == nil && a.model == nil && a.effort == nil {
		return nil, nil
	}
	if current == nil {
		return nil, failed("the run's lead is not known yet, so there is nothing to change it from")
	}
	spec := &pb.AgentSpec{Harness: current.GetHarness(), Model: current.GetModel(), ReasoningEffort: current.GetReasoningEffort()}
	if a.harness != nil && *a.harness != spec.Harness {
		spec = &pb.AgentSpec{Harness: *a.harness}
	}
	if a.model != nil {
		spec.Model = *a.model
	}
	if a.effort != nil {
		spec.ReasoningEffort = *a.effort
	}
	return spec, nil
}

// update changes the settings of a live run: its limits, its fan-out and its lead. The answer says what
// changed, as the run's own record shows it once the update is decided, and that nothing did when the run
// already had what was asked.
func update(ctx context.Context, c *call, args []string) error {
	a, arg, err := parseUpdate(args)
	if err != nil {
		return err
	}
	s, ctx, err := c.join(ctx, arg)
	if err != nil {
		return err
	}
	defer s.Close()
	run := s.view.Run
	if !live(run) {
		return failed("run %s has already %s, so there is nothing to change", run.GetId(), statusWord(run.GetStatus().String(), "RUN_STATUS_"))
	}
	lead, err := a.leadSpec(s.view.Lead())
	if err != nil {
		return err
	}
	was := settingsOf(s.view)
	receipt, err := c.dispatch(ctx, s, &pb.Command{Id: c.IDs.NewCommandID(), Body: &pb.Command_UpdateRun{UpdateRun: &pb.UpdateRun{
		RunId: run.GetId(), CostLimit: a.cost, TimeLimit: a.span, FanoutLimit: a.fanout, Lead: lead,
	}}})
	if err != nil {
		return err
	}
	if a.json {
		return printJSON(c.Stdout, receipt)
	}
	for s.view.Sequence < receipt.GetSequence() {
		if _, err := s.feed.next(s.view); err != nil {
			return err
		}
	}
	lines := was.changes(settingsOf(s.view))
	if len(lines) == 0 {
		lines = []string{"nothing changed: it already has those settings"}
	}
	for _, line := range lines {
		if _, err := fmt.Fprintf(c.Stdout, "run %s: %s\n", run.GetId(), line); err != nil {
			return err
		}
	}
	return nil
}

// runSettings are the settings an update can change, as a view of the run shows them.
type runSettings struct {
	cost    *float64
	total   time.Duration
	fanout  uint32
	lead    string
	pending string
	limit   pb.RunLimit
}

func settingsOf(v *View) runSettings {
	r := v.Run
	out := runSettings{cost: r.MaxCostUsd, fanout: r.GetFanoutLimit(), limit: r.GetLimitReached(), lead: specWords(v.Lead().GetHarness(), v.Lead().GetModel())}
	if r.GetDeadline() != nil {
		out.total = r.GetDeadline().AsTime().Sub(r.GetCreatedAt().AsTime())
	}
	if p := r.GetPendingLead(); p != nil {
		out.pending = specWords(p.GetHarness(), p.GetModel())
	}
	return out
}

// specWords names a lead: its tool and, when it has one, its model.
func specWords(h pb.Harness, model string) string {
	if model == "" {
		return names.Spell(h)
	}
	return names.Spell(h) + " " + model
}

// changes says, a line to a setting, what differs between two readings of a run.
func (was runSettings) changes(now runSettings) []string {
	var lines []string
	money := func(c *float64) string {
		if c == nil {
			return noLimit
		}
		return dollars(*c)
	}
	span := func(d time.Duration) string {
		if d == 0 {
			return noLimit
		}
		return d.String()
	}
	if money(was.cost) != money(now.cost) {
		lines = append(lines, fmt.Sprintf("max cost %s -> %s", money(was.cost), money(now.cost)))
	}
	if was.total != now.total {
		lines = append(lines, fmt.Sprintf("max time %s -> %s", span(was.total), span(now.total)))
	}
	if was.fanout != now.fanout {
		lines = append(lines, fmt.Sprintf("fanout %d -> %d, and the agents already live keep going", was.fanout, now.fanout))
	}
	switch {
	case was.pending == now.pending:
	case now.pending != "":
		lines = append(lines, fmt.Sprintf("lead %s -> %s, once its current turn ends", now.lead, now.pending))
	default:
		lines = append(lines, "lead change withdrawn: it stays "+now.lead)
	}
	if was.limit == pb.RunLimit_RUN_LIMIT_UNSPECIFIED && now.limit != pb.RunLimit_RUN_LIMIT_UNSPECIFIED {
		lines = append(lines, fmt.Sprintf("the run has already passed its %s limit, so its lead is told to finish", attachview.LimitWord(now.limit)))
	}
	return lines
}
