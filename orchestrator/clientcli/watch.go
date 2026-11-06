package clientcli

import (
	"context"
	"fmt"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli/attachview"
)

// attach follows one run, showing what it did and what it does, until the run ends or the
// caller detaches. A run that has already ended is only replayed. See conversation.go for what is shown.
func attach(ctx context.Context, c *call, args []string) error {
	fs := flags("attach")
	agent := fs.String("agent", "", "start in the conversation of this agent (a name such as worker-1, or an id), and with no terminal follow only it")
	full := fs.Bool("full-history", false, "replay every item of each agent viewed, not only the newest 200")
	positional, err := parse(fs, args)
	switch {
	case err != nil:
		return err
	case len(positional) > 1:
		return usageError("run attach takes at most one RUN, got %d arguments", len(positional))
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
	start := ""
	if *agent != "" {
		if start, err = s.view.ResolveAgent(*agent); err != nil {
			return failed("%v", err)
		}
	}
	history := attachview.DefaultHistory
	if *full {
		history = 0
	}
	return c.watchRun(ctx, s, start, history)
}

// watchRun shows the session's history, then each change as it comes. Ctrl-C (a cancelled ctx)
// only detaches: nothing here can stop a run. It returns nil when the run ended normally.
func (c *call) watchRun(ctx context.Context, s *session, start string, history int) error {
	run := s.view.RunID
	if s.view.Over() {
		fmt.Fprintf(c.Stderr, "run %s (task %s in %s) has %s; this is what it did.\n", run, s.view.Run.GetTask(), s.view.Run.GetRepository(), statusWord(s.view.Run.GetStatus().String(), "RUN_STATUS_"))
	} else {
		fmt.Fprintf(c.Stderr, "attached to run %s (task %s in %s). Ctrl-C detaches and leaves the run going.\n", run, s.view.Run.GetTask(), s.view.Run.GetRepository())
	}
	return c.converse(ctx, s, start, history)
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
