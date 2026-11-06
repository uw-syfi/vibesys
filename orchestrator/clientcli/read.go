package clientcli

import (
	"context"
	"fmt"
	"text/tabwriter"
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// list shows every run the service knows, newest first.
func list(ctx context.Context, c *call, args []string) error {
	fs := flags("list")
	asJSON := fs.Bool("json", false, "print the runs as JSON")
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
	snapshot, err := conn.Snapshot(ctx)
	if err != nil {
		return classify(err)
	}
	runs := snapshot.GetRuns()
	sortNewestFirst(runs)
	if *asJSON {
		return printJSON(c.Stdout, &pb.RunsSnapshot{Runs: runs, Sequence: snapshot.GetSequence(), Truncated: snapshot.GetTruncated()})
	}
	table := tabwriter.NewWriter(c.Stdout, 0, 2, 2, ' ', 0)
	fmt.Fprintln(table, "ID\tSTATUS\tTASK\tAGE\tREPOSITORY")
	for _, r := range runs {
		fmt.Fprintf(table, "%s\t%s\t%s\t%s\t%s\n", r.GetId(), statusWord(r.GetStatus().String(), "RUN_STATUS_"), r.GetTask(),
			age(c.Clock.Now(), r.GetCreatedAt().AsTime()), r.GetRepository())
	}
	if snapshot.GetTruncated() {
		fmt.Fprintln(table, "(older runs were left out of the snapshot)")
	}
	return table.Flush()
}

// get shows one run as it stands now.
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
