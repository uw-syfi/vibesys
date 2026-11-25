package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"path/filepath"
	"regexp"
	"slices"
	"strings"

	"github.com/uw-syfi/vibesys/cli/envcodec"
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
	// alloc is the standing allocation a job a submit queues runs in: its name, or "any".
	alloc string
	// environment is why the context variables could not be read, which a command that needs the
	// context says when it builds the call's context.
	environment error
}

// newFlags makes the flag set of a command, with the common flags in it. Flag errors are
// returned to the caller, which prints them once.
func newFlags(name string, d deps) (*flag.FlagSet, *common) {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	// The flag package writes an error to its output and then asks for the usage. The output holds
	// the error until then, so that the usage says it in the CLI's spelling (twoDashes) before the
	// flags; under --json, parseFailed says it as a Failure instead.
	said := &strings.Builder{}
	fs.SetOutput(said)
	fs.Usage = func() {
		message := said.String()
		said.Reset()
		if d.jsonErrors {
			return
		}
		_, _ = io.WriteString(d.stderr, twoDashes(message))
		printUsage(d.stderr, fs)
	}
	c := &common{}
	fs.BoolVar(&c.json, "json", false, "print the result as JSON")
	// The defaults come from the environment through the codec, so the names are the fields' own.
	agent, operator := &cliv1.AgentContext{}, &cliv1.OperatorContext{}
	c.environment = errors.Join(envcodec.Decode(d.getenv, agent), envcodec.Decode(d.getenv, operator))
	fs.StringVar(&c.repo, "repo", agent.GetRepository(), "the git repository (default: the one containing the current directory)")
	fs.StringVar(&c.task, "task", agent.GetTask(), "the task the jobs belong to")
	fs.StringVar(&c.run, "run", agent.GetRunId(), "the id of the optimization run")
	fs.StringVar(&c.harness, "harness", agent.GetHarness(), "the agent harness, such as claude-code")
	fs.StringVar(&c.agent, "agent", agent.GetAgent(), "the agent making the call")
	fs.StringVar(&c.placement, "placement", operator.GetPlacement(), "submit: the one placement of the operator config a docker job may run on (default: any that gives what the task needs, chosen when it starts)")
	fs.StringVar(&c.alloc, "alloc", "", "submit: run the job in the standing allocation of the run with this name, such as a7, which refuses the job unless it fits; or \"any\", which uses a running allocation it fits and otherwise queues it as a job with no allocation is queued")
	return fs, c
}

// A flag parses with one dash or two (-timeout, --timeout), but the flag package spells it with one
// in its errors and its list of flags, while this CLI's usage, its errors and its README spell it
// with two. twoDashes rewrites what the flag package says into the CLI's spelling, so a caller who
// copies a flag from an error or from -h types what the rest of the CLI shows.
var oneDash = regexp.MustCompile(`(^|\s)-([A-Za-z])`)

func twoDashes(text string) string { return oneDash.ReplaceAllString(text, "$1--$2") }

// printUsage writes a command's flags, each spelled as twoDashes spells it.
func printUsage(w io.Writer, fs *flag.FlagSet) {
	var defaults strings.Builder
	out := fs.Output()
	fs.SetOutput(&defaults)
	fs.PrintDefaults()
	fs.SetOutput(out)
	_, _ = fmt.Fprintf(w, "Usage of %s:\n", fs.Name())
	// Only the flag's own name starts a line; its help text is left as it is written.
	for _, line := range strings.SplitAfter(defaults.String(), "\n") {
		if strings.HasPrefix(line, "  -") {
			line = "  --" + strings.TrimPrefix(line, "  -")
		}
		_, _ = io.WriteString(w, line)
	}
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

// commandFirst reads the context flags written before the command, which newFlags gives every
// command, and moves them after it, so that `vibesys-cli --task kv jobs` is `vibesys-cli jobs --task
// kv`. They go after the command's own words and flags and before a lone "--", where every command
// reads its flags, since a command may read a word first (submit's kind, plan's verb). A flag given
// both before and after the command is refused, so neither place silently wins, and so is any other
// flag before the command, which only the command can read.
func commandFirst(args []string, d deps) ([]string, error) {
	context, _ := newFlags("context", d)
	var moved, names []string
	i := 0
	for i < len(args) && args[i] != "--" && args[i] != "-" && strings.HasPrefix(args[i], "-") {
		name, _, hasValue := strings.Cut(strings.TrimPrefix(strings.TrimPrefix(args[i], "-"), "-"), "=")
		f := context.Lookup(name)
		if f == nil {
			return nil, usagef("%s comes before the command, where only the context flags (%s) may; put flags after the subcommand: vibesys-cli <cmd> --task ...", args[i], contextNames(context))
		}
		moved, names = append(moved, args[i]), append(names, name)
		i++
		if boolean, ok := f.Value.(interface{ IsBoolFlag() bool }); !hasValue && (!ok || !boolean.IsBoolFlag()) {
			if i == len(args) {
				return nil, usagef("--%s: give it a value, then the command", name)
			}
			moved = append(moved, args[i])
			i++
		}
	}
	if len(moved) == 0 {
		return args, nil
	}
	if i == len(args) || args[i] == "--" {
		return nil, usagef("name a command after %s", strings.Join(moved, " "))
	}
	rest, after, found := splitDashes(args[i:])
	for _, name := range names {
		if slices.ContainsFunc(rest, func(a string) bool { return namesFlag(a, name) }) {
			return nil, usagef("--%s is given before the command and after it; give it once, after the command", name)
		}
	}
	out := slices.Concat(rest, moved)
	if found {
		out = slices.Concat(out, []string{"--"}, after)
	}
	return out, nil
}

// namesFlag says whether a command-line word sets the flag of that name, in any spelling the flag
// package reads: -name, --name, -name=v or --name=v.
func namesFlag(word, name string) bool {
	for _, dashes := range []string{"-", "--"} {
		if word == dashes+name || strings.HasPrefix(word, dashes+name+"=") {
			return true
		}
	}
	return false
}

// contextNames lists the context flags, as a caller types them.
func contextNames(context *flag.FlagSet) string {
	var names []string
	context.VisitAll(func(f *flag.Flag) { names = append(names, "--"+f.Name) })
	return strings.Join(names, ", ")
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
	if c.environment != nil {
		return nil, usagef("the environment: %v", c.environment)
	}
	if c.task == "" {
		return nil, usagef("--task: name the task (or set %s)", envcodec.NameOf(&cliv1.AgentContext{}, "task"))
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
		printUsage(d.stderr, fs)
		return exitOK
	case isHelp(err):
		return exitOK
	case d.jsonErrors:
		return fail(d, true, usagef("%s", twoDashes(err.Error())))
	}
	return exitInvalidInput
}

// errHelp reports that help was asked for, which is not a failure.
var errHelp = flag.ErrHelp

func isHelp(err error) bool { return errors.Is(err, flag.ErrHelp) }
