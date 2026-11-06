package clientcli

import (
	"context"
	"fmt"
	"text/tabwriter"
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// list shows the runs the service knows, newest first. A start that failed before its lead ran
// anything is not a run a person means to look at, so it is left out unless --all says otherwise;
// `run get RUN` still shows it.
func list(ctx context.Context, c *call, args []string) error {
	fs := flags("list")
	asJSON := fs.Bool("json", false, "print the runs as JSON")
	all := fs.Bool("all", false, "include starts that failed before their lead ran anything")
	positional, err := parse(fs, args)
	if err != nil {
		return err
	}
	if len(positional) > 0 {
		return usageError("run list takes no argument, got %q", positional[0])
	}
	conn, err := c.open(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	snapshot, err := within(ctx, c.Clock, serviceBound, "send the runs", conn.Snapshot)
	if err != nil {
		return classify(err)
	}
	runs := snapshot.GetRuns()
	sortNewestFirst(runs)
	hidden := 0
	if !*all {
		shown := runs[:0:0]
		for _, r := range runs {
			if r.GetNeverOpened() {
				hidden++
				continue
			}
			shown = append(shown, r)
		}
		runs = shown
	}
	if *asJSON {
		return printJSON(c.Stdout, &pb.RunsSnapshot{Runs: runs, Sequence: snapshot.GetSequence(), Truncated: snapshot.GetTruncated()})
	}
	table := tabwriter.NewWriter(c.Stdout, 0, 2, 2, ' ', 0)
	fmt.Fprintln(table, "ID\tSTATUS\tTASK\tAGE\tREPOSITORY")
	for _, r := range runs {
		status := statusWord(r.GetStatus().String(), "RUN_STATUS_")
		if r.GetNeverOpened() {
			status += " (never started)"
		}
		fmt.Fprintf(table, "%s\t%s\t%s\t%s\t%s\n", r.GetId(), status, r.GetTask(),
			age(c.Clock.Now(), r.GetCreatedAt().AsTime()), r.GetRepository())
	}
	if hidden > 0 {
		fmt.Fprintf(table, "(%d start(s) that failed before the agent ran are hidden; `run list --all` shows them, `run get RUN` explains one)\n", hidden)
	}
	if snapshot.GetTruncated() {
		fmt.Fprintln(table, "(older runs were left out of the snapshot)")
	}
	return table.Flush()
}

// get shows one run as it stands now: with no RUN, the live one here, or else the most recent one here.
func get(ctx context.Context, c *call, args []string) error {
	fs := flags("get")
	asJSON := fs.Bool("json", false, "print the run as a JSON RunsSnapshot of this run")
	positional, err := parse(fs, args)
	if err != nil {
		return err
	}
	if len(positional) > 1 {
		return usageError("run get takes at most one RUN, got %d arguments", len(positional))
	}
	arg := ""
	if len(positional) == 1 {
		arg = positional[0]
	}
	s, _, err := c.join(ctx, arg)
	if err != nil {
		return err
	}
	defer s.Close()
	if *asJSON {
		return printJSON(c.Stdout, s.view.Snapshot())
	}
	_, err = fmt.Fprint(c.Stdout, summarize(s.view, c.Clock.Now(), c.Zone))
	return err
}

// age is how long ago something was, in the largest whole unit.
func age(now, then time.Time) string {
	d := max(now.Sub(then), 0)
	switch {
	case d < time.Minute:
		return fmt.Sprintf("%ds", int(d.Seconds()))
	case d < time.Hour:
		return fmt.Sprintf("%dm", int(d.Minutes()))
	case d < 48*time.Hour:
		return fmt.Sprintf("%dh", int(d.Hours()))
	}
	return fmt.Sprintf("%dd", int(d.Hours()/24))
}
