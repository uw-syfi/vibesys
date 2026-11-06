package clientcli

import (
	"context"
	"fmt"
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// goals are what `run wait --until` can wait for.
var goals = map[string]func(*View) bool{
	"started": (*View).Started,
	"report":  func(v *View) bool { return len(v.Reports()) > 0 },
	"ended":   (*View).Over,
}

// wait returns once the run reaches the goal, and at once when it already has. It subscribes to the
// stream and never polls. A run that ends before the goal is a failure, and so is a run that
// ends in FAILED when the goal was its end.
func wait(ctx context.Context, c *call, args []string) error {
	fs := flags("wait")
	until := fs.String("until", "", "started, report or ended")
	timeout := fs.Duration("timeout", 0, "give up after this long, such as 90s or 30m")
	asJSON := fs.Bool("json", false, "print the run as a JSON RunsSnapshot of this run")
	positional, err := parse(fs, args)
	if err != nil {
		return err
	}
	met, ok := goals[*until]
	switch {
	case len(positional) > 1:
		return usageError("run wait takes at most one RUN, got %d arguments", len(positional))
	case !ok:
		return usageError("run wait needs --until started, report or ended")
	case *timeout <= 0:
		return usageError("run wait needs --timeout, such as --timeout 30m")
	}
	arg := ""
	if len(positional) == 1 {
		arg = positional[0]
	}
	bounded, expired, cancel := bound(ctx, c.Clock, *timeout)
	defer cancel()
	s, streamCtx, err := c.join(bounded, arg)
	if err != nil {
		return c.interrupted(ctx, expired, *until, *timeout, nil, err)
	}
	defer s.Close()
	for !met(s.view) {
		if s.view.Over() {
			return failed("run %s %s before %s", s.view.RunID, statusWord(s.view.Run.GetStatus().String(), "RUN_STATUS_"), untilWords[*until])
		}
		if _, err := s.feed.next(s.view); err != nil {
			return c.interrupted(streamCtx, expired, *until, *timeout, s.view, err)
		}
	}
	if *until == "ended" && s.view.Run.GetStatus() == pb.RunStatus_RUN_STATUS_FAILED {
		return failed("run %s failed: %s", s.view.RunID, orUnknown(s.view.failureDetail(s.view.Run)))
	}
	if *asJSON {
		return printJSON(c.Stdout, s.view.Snapshot())
	}
	_, err = fmt.Fprintln(c.Stdout, reached(s.view, *until))
	return err
}

var untilWords = map[string]string{"started": "its lead was live", "report": "a report was posted", "ended": "it ended"}

// interrupted explains why a stream read ended without the goal: the timeout, a caller that
// cancelled, or an error from the service.
func (c *call) interrupted(ctx context.Context, expired func() bool, until string, timeout time.Duration, v *View, err error) error {
	switch {
	case expired():
		where := ""
		if v != nil {
			where = fmt.Sprintf("; run %s is %s", v.RunID, statusWord(v.Run.GetStatus().String(), "RUN_STATUS_"))
		}
		return timedOut("timed out after %s waiting for %s%s", timeout, untilWords[until], where)
	case ctx.Err() != nil:
		return failed("interrupted")
	}
	return err
}

func reached(v *View, until string) string {
	switch until {
	case "started":
		return fmt.Sprintf("run %s: %s is live", v.RunID, agentLabel(v.Lead(), v.Run.GetLeadAgentId()))
	case "report":
		reports := v.Reports()
		return fmt.Sprintf("run %s: report: %s", v.RunID, oneLine(reports[len(reports)-1].GetReport().GetHeadline(), 300))
	}
	return fmt.Sprintf("run %s %s", v.RunID, statusWord(v.Run.GetStatus().String(), "RUN_STATUS_"))
}
