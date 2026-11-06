package clientcli

import (
	"bufio"
	"context"
	"fmt"
	"strings"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// steer sends text to a run's lead, or to the agent named by --agent.
func steer(ctx context.Context, c *call, args []string) error {
	fs := flags("steer")
	agent := fs.String("agent", "", "the agent to send to (default: the run's lead)")
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
	target := *agent
	if target == "" {
		target = s.view.Run.GetLeadAgentId()
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
	receipt, err := s.client.Dispatch(ctx, cmd)
	if err != nil {
		return nil, classify(err)
	}
	return receipt, refusal(receipt)
}

// delivery says in words where a steering message stands. A rejected one is read back from the
// stream, which carries every event up to the receipt, to give the reason.
func (c *call) delivery(ctx context.Context, s *session, receipt *pb.Receipt, target string) (string, error) {
	who := agentLabel(s.view.Agents[target], target)
	switch receipt.GetDelivery() {
	case pb.Delivery_DELIVERY_QUEUED:
		return fmt.Sprintf("queued: %s will see it when its current work ends", who), nil
	case pb.Delivery_DELIVERY_STEERED:
		return fmt.Sprintf("sent into the running turn of %s", who), nil
	case pb.Delivery_DELIVERY_NEW_TURN:
		return fmt.Sprintf("started a new turn for %s", who), nil
	case pb.Delivery_DELIVERY_CONSUMED:
		return fmt.Sprintf("%s has it", who), nil
	case pb.Delivery_DELIVERY_REJECTED:
		for s.view.Sequence < receipt.GetSequence() {
			if _, err := s.feed.next(s.view); err != nil {
				return "", err
			}
		}
		reason := statusWord(s.view.Messages[receipt.GetMessageId()].GetRejection().String(), "REJECTION_")
		return "", failed("%s did not take the message: %s", who, reason)
	}
	return fmt.Sprintf("accepted for %s", who), nil
}

// stop ends a run and every agent in it. A person at a terminal is asked first.
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
