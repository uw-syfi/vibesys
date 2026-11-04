package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"path/filepath"
	"slices"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
)

// usageError is a mistake in the arguments: the exit code is 2 and the message names it.
type usageError struct{ message string }

func (e usageError) Error() string { return e.message }

func usagef(format string, args ...any) error { return usageError{fmt.Sprintf(format, args...)} }

// common holds the flags every command that talks about a task takes. They are the context of a
// call, each read from its environment variable by default, so a caller sets them once for every
// command; a command that has no use for one, such as --placement outside submit, takes and
// ignores it.
type common struct {
	json    bool
	repo    string
	task    string
	run     string
	harness string
	agent   string
	// placement restricts a docker job a submit queues to one placement of the operator config.
	placement string
}

// newFlags makes the flag set of a command, with the common flags in it. Flag errors are
// returned to the caller, which prints them once.
func newFlags(name string, d deps) (*flag.FlagSet, *common) {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(d.stderr)
	if d.jsonErrors {
		fs.SetOutput(io.Discard) // parseFailed says it as a Failure.
	}
	c := &common{}
	fs.BoolVar(&c.json, "json", false, "print the result as JSON")
	fs.StringVar(&c.repo, "repo", d.getenv("VIBESYS_REPO"), "the git repository (default: the one containing the current directory)")
	fs.StringVar(&c.task, "task", d.getenv("VIBESYS_TASK"), "the task the jobs belong to")
	fs.StringVar(&c.run, "run", d.getenv("VIBESYS_RUN"), "the id of the optimization run")
	fs.StringVar(&c.harness, "harness", d.getenv("VIBESYS_HARNESS"), "the agent harness, such as claude-code")
	fs.StringVar(&c.agent, "agent", d.getenv("VIBESYS_AGENT"), "the agent making the call")
	fs.StringVar(&c.placement, "placement", d.getenv("VIBESYS_PLACEMENT"), "submit: the one placement of the operator config a docker job may run on (default: any that gives what the task needs, chosen when it starts)")
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

// callContext makes the call context of a command: the directory it runs in and the task it is
// about. A task is required, since the daemon serves many. The daemon resolves the directory to its
// repository, so the command line does not ask git which repository it is.
func (c *common) callContext(d deps) (*cliv1.CallContext, error) {
	if c.task == "" {
		return nil, usagef("--task: name the task (or set VIBESYS_TASK)")
	}
	dir := c.repo
	switch {
	case dir == "":
		dir = d.cwd
	case !filepath.IsAbs(dir):
		dir = filepath.Join(d.cwd, dir)
	}
	return &cliv1.CallContext{Repository: filepath.Clean(dir), Task: c.task, RunId: c.run, Harness: c.harness, Agent: c.agent}, nil
}

// commit returns the full commit that revision names for this command: read in the worktree the
// command runs in when that is a worktree of the context's repository, so that HEAD is the caller's
// own HEAD, and in the context's repository otherwise (gitrepo.CallerCommit). Every command that
// takes a commit resolves it here, and sends the full commit id it returns.
func (d deps) commit(ctx context.Context, callCtx *cliv1.CallContext, revision string) (string, error) {
	commit, err := gitrepo.CallerCommit(ctx, d.git, callCtx.GetRepository(), d.cwd, revision)
	return string(commit), err
}

// enumNames maps the names a user writes to a contract enum's values: each value's name, lower
// case, without the prefix, leaving out the unspecified value. It is read from the enum, so a value
// added there is a name here.
func enumNames[E ~int32](names map[int32]string, prefix string, of func(int32) E) map[string]E {
	out := map[string]E{}
	for n, name := range names {
		if n != 0 {
			out[strings.ToLower(strings.TrimPrefix(name, prefix))] = of(n)
		}
	}
	return out
}

// choices lists an enum's names as enumNames reads them, in the order the contract numbers them.
func choices(names map[int32]string, prefix string) string {
	numbers := make([]int32, 0, len(names))
	for n := range names {
		if n != 0 {
			numbers = append(numbers, n)
		}
	}
	slices.Sort(numbers)
	out := make([]string, 0, len(numbers))
	for _, n := range numbers {
		out = append(out, strings.ToLower(strings.TrimPrefix(names[n], prefix)))
	}
	return strings.Join(out, ", ")
}

// stateNames maps the names a user writes to job states.
var stateNames = enumNames(cliv1.JobState_name, "JOB_STATE_", func(n int32) cliv1.JobState { return cliv1.JobState(n) })

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
			return fmt.Errorf("%q is not a job state (%s)", name, choices(cliv1.JobState_name, "JOB_STATE_"))
		}
		*l = append(*l, s)
	}
	return nil
}

// wantsJSON says whether a command line asks for JSON. It is for what goes wrong before the command
// has read its flags, so it reads the arguments itself. What follows a lone "--" is the job's.
func wantsJSON(args []string) bool {
	before, _, _ := splitDashes(args)
	return slices.ContainsFunc(before, func(a string) bool {
		switch a {
		case "--json", "-json", "--json=true", "-json=true":
			return true
		}
		return false
	})
}

// parseFailed ends a command whose flags did not parse and returns its exit code. The flag package
// has printed the error, unless --json asks for a Failure instead.
func parseFailed(d deps, fs *flag.FlagSet, err error) int {
	switch {
	case isHelp(err) && d.jsonErrors:
		fs.SetOutput(d.stderr)
		fs.Usage()
		return exitOK
	case isHelp(err):
		return exitOK
	case d.jsonErrors:
		return fail(d, true, usagef("%v", err))
	}
	return exitInvalidInput
}

// errHelp reports that help was asked for, which is not a failure.
var errHelp = flag.ErrHelp

func isHelp(err error) bool { return errors.Is(err, flag.ErrHelp) }
