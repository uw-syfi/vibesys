package clientcli

import (
	"context"
	"fmt"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// attach follows one run, showing what it did and what it does, until the run ends or the
// caller detaches. A run that has already ended is only replayed.
func attach(ctx context.Context, c *call, args []string) error {
	arg, err := optionalRun(args, "attach")
	if err != nil {
		return err
	}
	s, ctx, err := c.join(ctx, arg)
	if err != nil {
		return err
	}
	defer s.Close()
	return c.watchRun(ctx, s)
}

// optionalRun reads the arguments of a verb that takes only an optional RUN.
func optionalRun(args []string, verb string) (string, error) {
	positional, err := parse(flags(verb), args)
	switch {
	case err != nil:
		return "", err
	case len(positional) > 1:
		return "", usageError("run %s takes at most one RUN, got %d arguments", verb, len(positional))
	case len(positional) == 1:
		return positional[0], nil
	}
	return "", nil
}

// watchRun shows the session's history, then each change as it comes. Ctrl-C (a cancelled ctx)
// only detaches: nothing here can stop a run. It returns nil when the run ended normally.
func (c *call) watchRun(ctx context.Context, s *session) error {
	run := s.view.RunID
	if s.view.Over() {
		fmt.Fprintf(c.Stderr, "run %s (task %s in %s) has %s; this is what it did.\n", run, s.view.Run.GetTask(), s.view.Run.GetRepository(), statusWord(s.view.Run.GetStatus().String(), "RUN_STATUS_"))
	} else {
		fmt.Fprintf(c.Stderr, "attached to run %s (task %s in %s). Ctrl-C detaches and leaves the run going.\n", run, s.view.Run.GetTask(), s.view.Run.GetRepository())
	}
	c.show(s.view, s.history)
	for !s.view.Over() {
		changes, err := s.feed.next(s.view)
		if err != nil {
			if ctx.Err() != nil {
				c.detached(run)
				return nil
			}
			return err
		}
		c.show(s.view, changes)
	}
	if s.view.Run.GetStatus() == pb.RunStatus_RUN_STATUS_FAILED {
		return &failure{code: ExitFailed}
	}
	return nil
}

func (c *call) show(v *View, changes []Change) {
	for _, change := range changes {
		fmt.Fprintln(c.Stdout, v.Describe(change, c.Zone))
	}
}

// detached tells the caller the run goes on, and how to come back to it or end it.
func (c *call) detached(run string) {
	fmt.Fprintf(c.Stderr, "detached: run %s is still going.\n  %s\n  %s\n", run, c.command("attach", run), c.command("stop", run))
}

// command is the command line that runs a verb on a run, with the --root this call was given.
func (c *call) command(verb, run string) string {
	root := ""
	if c.root != "" {
		root = " --root " + c.root
	}
	return fmt.Sprintf("%s%s run %s %s", Program, root, verb, run)
}
