package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// usageError is a mistake in the arguments: the exit code is 2 and the message names it.
type usageError struct{ message string }

func (e usageError) Error() string { return e.message }

func usagef(format string, args ...any) error { return usageError{fmt.Sprintf(format, args...)} }

// common holds the flags every command that talks about a task takes.
type common struct {
	json    bool
	repo    string
	task    string
	run     string
	harness string
	agent   string
}

// newFlags makes the flag set of a command, with the common flags in it. Flag errors are
// returned to the caller, which prints them once.
func newFlags(name string, d deps) (*flag.FlagSet, *common) {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(d.stderr)
	c := &common{}
	fs.BoolVar(&c.json, "json", false, "print the result as JSON")
	fs.StringVar(&c.repo, "repo", d.getenv("VIBESYS_REPO"), "the git repository (default: the one containing the current directory)")
	fs.StringVar(&c.task, "task", d.getenv("VIBESYS_TASK"), "the task the jobs belong to")
	fs.StringVar(&c.run, "run", d.getenv("VIBESYS_RUN"), "the id of the optimization run")
	fs.StringVar(&c.harness, "harness", d.getenv("VIBESYS_HARNESS"), "the agent harness, such as claude-code")
	fs.StringVar(&c.agent, "agent", d.getenv("VIBESYS_AGENT"), "the agent making the call")
	return fs, c
}

// parse reads flags and positional arguments in any order: wait j1 j2 --any is as good as
// wait --any j1 j2. Arguments after a lone "--" are not read here; the caller split them off.
func parse(fs *flag.FlagSet, args []string) ([]string, error) {
	var positional []string
	for {
		if err := fs.Parse(args); err != nil {
			return nil, err
		}
		rest := fs.Args()
		if len(rest) == 0 {
			return positional, nil
		}
		positional = append(positional, rest[0])
		args = rest[1:]
	}
}

// splitDashes splits the arguments at the first lone "--".
func splitDashes(args []string) (before, after []string, found bool) {
	for i, a := range args {
		if a == "--" {
			return args[:i], args[i+1:], true
		}
	}
	return args, nil, false
}

// callContext makes the call context of a command: the repository it runs in and the task it is
// about. A task is required, since the daemon serves many.
func (c *common) callContext(ctx context.Context, d deps) (*cliv1.CallContext, error) {
	if c.task == "" {
		return nil, usagef("--task: name the task (or set VIBESYS_TASK)")
	}
	repo := c.repo
	if repo == "" {
		repo = d.cwd
	}
	top, err := d.git.Toplevel(ctx, repo)
	if err != nil {
		return nil, err
	}
	return &cliv1.CallContext{Repository: top, Task: c.task, RunId: c.run, Harness: c.harness, Agent: c.agent}, nil
}

// stateNames maps the names a user writes to job states.
var stateNames = map[string]cliv1.JobState{
	"queued":    cliv1.JobState_JOB_STATE_QUEUED,
	"running":   cliv1.JobState_JOB_STATE_RUNNING,
	"succeeded": cliv1.JobState_JOB_STATE_SUCCEEDED,
	"failed":    cliv1.JobState_JOB_STATE_FAILED,
	"cancelled": cliv1.JobState_JOB_STATE_CANCELLED,
	"lost":      cliv1.JobState_JOB_STATE_LOST,
}

func stateName(s cliv1.JobState) string {
	return strings.ToLower(strings.TrimPrefix(s.String(), "JOB_STATE_"))
}

// stateList is a repeatable flag that also takes a comma-separated list.
type stateList []cliv1.JobState

func (l *stateList) String() string { return fmt.Sprint([]cliv1.JobState(*l)) }

func (l *stateList) Set(v string) error {
	for _, name := range strings.Split(v, ",") {
		s, ok := stateNames[strings.TrimSpace(name)]
		if !ok {
			return fmt.Errorf("%q is not a job state (queued, running, succeeded, failed, cancelled or lost)", name)
		}
		*l = append(*l, s)
	}
	return nil
}

// errHelp reports that help was asked for, which is not a failure.
var errHelp = flag.ErrHelp

func isHelp(err error) bool { return errors.Is(err, flag.ErrHelp) }
