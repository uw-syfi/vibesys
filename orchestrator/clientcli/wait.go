package clientcli

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// goal is something `run wait --until` can wait for.
type goal struct {
	// words finishes the sentence "waiting for ..." in a timeout and "... before" in a failure.
	words string
	// met says the view has reached it. after is the number of the last report that does not count.
	met func(v *View, after uint32) bool
	// reports says the goal is about reports, so --after means something to it.
	reports bool
}

var goals = map[string]goal{
	"started": {words: "its lead was live", met: func(v *View, _ uint32) bool { return v.Started() }},
	"report": {words: "a final report was posted", reports: true, met: func(v *View, after uint32) bool {
		return v.ReportAfter(after, true) != nil
	}},
	"progress": {words: "a report was posted", reports: true, met: func(v *View, after uint32) bool {
		return v.ReportAfter(after, false) != nil
	}},
	"stalled": {words: "the run to stall", met: func(v *View, _ uint32) bool { return v.Stalled() }},
	"ended":   {words: "it ended", met: func(v *View, _ uint32) bool { return v.Over() }},
}

// untilNames lists the goals for a message.
func untilNames() string {
	names := make([]string, 0, len(goals))
	for name := range goals {
		names = append(names, name)
	}
	sort.Strings(names)
	return strings.Join(names, ", ")
}

// reportBaseline is the number of the last report that a wait does not count. --after names it. With
// no --after, a wait for any report counts those posted after it began, so that a progress note the
// caller has already read does not end it. A wait for the final report counts the one the run already
// has, since there is only that one to wait for: a caller that joins late gets it, and so does one
// that waits on a run that has ended. A run with no final report yet is waited on from now.
func reportBaseline(v *View, after int, finalOnly bool) uint32 {
	switch {
	case after >= 0:
		return uint32(after)
	case v.Over():
		return 0
	case finalOnly && v.ReportAfter(0, true) != nil:
		return 0
	}
	return v.Run.GetReportsPosted()
}

// wait returns once the run reaches the goal, and at once when it already has. It subscribes to the
// stream and never polls. A run that ends before the goal is a failure, and so is a run that
// ends in FAILED when the goal was its end.
func wait(ctx context.Context, c *call, args []string) error {
	fs := flags("wait")
	until := fs.String("until", "", untilNames())
	timeout := fs.Duration("timeout", 0, "give up after this long, such as 90s or 30m")
	after := fs.Int("after", -1, "with report or progress: count only reports numbered above this")
	asJSON := fs.Bool("json", false, "print the run as a JSON RunsSnapshot of this run")
	positional, err := parse(fs, args)
	if err != nil {
		return err
	}
	g, ok := goals[*until]
	switch {
	case len(positional) > 1:
		return usageError("run wait takes at most one RUN, got %d arguments", len(positional))
	case !ok:
		return usageError("run wait needs --until %s", untilNames())
	case *timeout <= 0:
		return usageError("run wait needs --timeout, such as --timeout 30m")
	case *after >= 0 && !g.reports:
		return usageError("--after goes with --until report or progress, not %s", *until)
	}
	arg := ""
	if len(positional) == 1 {
		arg = positional[0]
	}
	bounded, expired, cancel := bound(ctx, c.Clock, *timeout)
	defer cancel()
	s, streamCtx, err := c.join(bounded, arg, onlyLive)
	if err != nil {
		return c.interrupted(ctx, expired, g, *timeout, nil, c.Clock.Now(), err)
	}
	defer s.Close()
	baseline := reportBaseline(s.view, *after, *until == "report")
	// The caller is told what is being waited for and what state the run is in, on stderr: stdout is
	// the result, and an unattended caller sees at once that the wait began and on which run.
	fmt.Fprintf(c.Stderr, "waiting for %s on run %s (%s)\n", g.words, s.view.RunID, statusWord(s.view.Run.GetStatus().String(), "RUN_STATUS_")+stallWords(s.view, c.Clock.Now()))
	for !g.met(s.view, baseline) {
		if s.view.Over() {
			return failed("run %s %s before %s", s.view.RunID, statusWord(s.view.Run.GetStatus().String(), "RUN_STATUS_"), g.words)
		}
		if _, err := s.feed.next(s.view); err != nil {
			return c.interrupted(streamCtx, expired, g, *timeout, s.view, c.Clock.Now(), err)
		}
	}
	if *until == "ended" && s.view.Run.GetStatus() == pb.RunStatus_RUN_STATUS_FAILED {
		return c.runFailed(s.view)
	}
	if *asJSON {
		return printJSON(c.Stdout, s.view.Snapshot())
	}
	_, err = fmt.Fprintln(c.Stdout, reached(s.view, *until, baseline, c.Zone))
	return err
}

// interrupted explains why a stream read ended without the goal: the timeout, a caller that
// cancelled, or an error from the service. A timeout says if the run was standing still, since a
// caller that waits for a report from a run that stalled will not get one.
func (c *call) interrupted(ctx context.Context, expired func() bool, g goal, timeout time.Duration, v *View, now time.Time, err error) error {
	switch {
	case expired():
		where := ""
		if v != nil {
			where = fmt.Sprintf("; run %s is %s", v.RunID, statusWord(v.Run.GetStatus().String(), "RUN_STATUS_")+stallWords(v, now))
		}
		return timedOut("timed out after %s waiting for %s%s", timeout, g.words, where)
	case ctx.Err() != nil:
		return failed("interrupted")
	}
	return err
}

func reached(v *View, until string, baseline uint32, zone *time.Location) string {
	switch until {
	case "started":
		return fmt.Sprintf("run %s: %s is live", v.RunID, agentLabel(v.Lead(), v.Run.GetLeadAgentId()))
	case "report", "progress":
		m := v.ReportAfter(baseline, until == "report")
		r := m.GetReport()
		return fmt.Sprintf("run %s: %s report %d: %s", v.RunID, reportWord(r.GetKind()), r.GetSequence(), oneLine(r.GetHeadline(), 300))
	case "stalled":
		return fmt.Sprintf("run %s is stalled: its lead has been idle since %s with nothing outstanding", v.RunID, clock(v.Run.GetStalledSince().AsTime(), zone))
	}
	return fmt.Sprintf("run %s %s", v.RunID, statusWord(v.Run.GetStatus().String(), "RUN_STATUS_"))
}
