package clientcli

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"strings"

	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
)

// Program is the executable's name.
const Program = "vibesys-client"

// Usage is the text printed for a usage error and for --help.
const Usage = `usage: vibesys-client [--root PATH] run VERB ... | host VERB ... | release verify ...
  run start --task T [--repository DIR] [--brief TEXT] [--harness claude_code|codex] [--fanout N]
            [--model M] [--effort E] [--allow-agent HARNESS[:MODEL]]... [--max-time DURATION] [--max-cost DOLLARS]
            [--summary-every DURATION] [--detach] [--json]
  run list [--all] [--json]
  run attach [RUN] [--agent AGENT] [--full-history]
  run get [RUN] [--json]
  run wait [RUN] --until started|report|progress|stalled|ended --timeout DURATION [--after N] [--json]
  run steer [RUN] TEXT [--agent AGENT] [--json]
  run stop [RUN] [--yes]
  host status [--json]
  host stop [--if-no-live-runs] [--force]
  host restart
  host configure [--state-directory DIR] [--session-holder CMD] [--skills-directory DIR]
                 [--job-tool PATH] [--tool-path DIR:DIR...]
                 [--repositories-directory DIR] [--clear]
  release verify [--installed] DIR
  --version

run attach shows what the run does as lines in the scrollback: the lead's replies in full, each tool call as
one line ($ command, read PATH, write PATH, edit A, B, search PATTERN, or the tool's name and first line;
output is hidden, except that a failed call shows its exit code and one line), a line when each turn starts
(what started it) and ends (tool calls, cost, output tokens), and the other agents and the run's milestones
as short lines. Agents are named by role and number in the order they started: lead, planner-1, worker-2.
run steer --agent takes these names, or an agent's full id, which the agent's view and run get --json
show. At a terminal a footer shows the elapsed time, active agents, cost and the keys: a lists the agents
with numbers, a number opens that agent's conversation (replayed, then followed), Esc or b goes up to the
agent that started it and on to the lead, and Ctrl-C detaches whatever the view and leaves the run going.
With input or output that is not a terminal there are no keys and no footer, and the same lines are
printed. --agent starts in the conversation of that agent (a name such as worker-1, or an id), and without
a terminal follows only it. A replay holds the newest 200 items of the agent viewed and says how many it
left out; --full-history replays them all. run attach has no --json: its lines are the output.
At a terminal the lines are lightly coloured (tool calls dim, failures red, prompts cyan, outcomes green or
yellow) with the terminal's own 16 colours; NO_COLOR set to anything, or TERM=dumb, turns it off, and with
input or output that is not a terminal the bytes are plain text.
A run starts subagents only on the lead's harness and model, and on each --allow-agent: a harness alone
allows its default model. A spawn outside that is refused, and run get shows what is allowed.
A run has no limit unless run start is given one: --max-time (such as 30m) and --max-cost (dollars). When a
limit is reached the lead is told once to finish and post its final report, a spawn is refused naming the
limit, and the run stops when the lead has posted the final report, or 10 minutes after the lead was told.
run get and run wait then say the run ended by its time limit or cost limit. The host counts the time from
the start. The cost is the sum of what the agents' finished turns reported, so a run can pass --max-cost by
what is in flight when the limit is reached, and a turn whose provider reports no cost adds nothing: for a
model the agent library cannot price, --max-cost never takes effect, and run get says so once a turn has
finished with no cost reported.
While a run's work moves, its lead is asked for a progress report about every hour, or every --summary-every
(at least 5 minutes, such as 30m): a lead that reports on its own restarts the interval, and a run where nothing
moved is not asked.
RUN is a run id, a unique prefix of one, or omitted for the one live run in this git repository. When
no run is live there, an omitted RUN means its most recent run, for every verb but steer: get shows it,
attach replays it, wait returns at once as for any RUN that has ended, and stop says the run has
already ended and exits 0. steer to a run that has ended is an error, since nothing would read the message.
run start and host restart are the only verbs that replace a host, and only once a new one is shown to
be able to start: run start replaces one that is older, another build or on another configuration, when
it has no live run and no other client; host restart replaces any host with no live run. No other verb
stops a host that serves, or starts one unless none is there. host status and host stop never start one.
host stop --force ends the process that holds the host's lock, for a host that does not answer.
A host on another machine, seen through a shared disk, is named, and nothing here touches it.
host configure records what the next host starts with, refusing what it can tell no host could use, and
with no flag prints it. --clear never reads the file, so it also puts right one that does not read.
release verify checks the release in DIR against its manifest, prints its version, and needs no host.
wait --until report returns for a final report and --until progress for any report. Reports are numbered
from 1 (run get --json shows the numbers). --until report returns at once with the final report a run
already has, however late the wait begins, and otherwise waits for the next one; --until progress counts
the reports posted after the wait began, and for a run that has ended the report it already has. --after N
counts only reports numbered above N, for either. --until stalled returns when the run stands still: its
lead is idle and no agent, job or message is outstanding. A wait for a report does not return for
a stalled run: it runs to its timeout, which then says the run is stalled.
Exit codes: 0 ok, 1 failed or refused, 2 usage, 3 timeout, 4 service unavailable.
`

// Main runs one command line and returns the process exit code. Cancelling ctx detaches: no
// verb stops a run because its caller was interrupted.
func Main(ctx context.Context, args []string, env Env) int {
	err := execute(ctx, args, env)
	if err == nil {
		return ExitOK
	}
	if errors.Is(err, flag.ErrHelp) {
		fmt.Fprint(env.Stdout, Usage)
		return ExitOK
	}
	var f *failure
	if errors.As(err, &f) && f.msg == "" {
		return f.code // the verb already said everything on stdout.
	}
	fmt.Fprintf(env.Stderr, "%s: %v\n", Program, err)
	if errors.As(err, &f) && f.usage {
		fmt.Fprint(env.Stderr, "\n"+Usage)
	}
	return exitCodeOf(err)
}

func execute(ctx context.Context, args []string, env Env) error {
	root := ""
	if len(args) > 0 && (args[0] == "--root" || args[0] == "-root") {
		if len(args) < 2 {
			return usageError("--root needs a path")
		}
		root, args = args[1], args[2:]
	}
	if len(args) == 0 {
		return usageError("a verb is needed")
	}
	if len(args) == 1 && args[0] == "--version" {
		_, err := fmt.Fprintf(env.Stdout, "%s %s\n", Program, env.Version)
		return err
	}
	if args[0] == "-h" || args[0] == "--help" || args[0] == "help" {
		return flag.ErrHelp
	}
	if args[0] == "release" && len(args) >= 2 && args[1] == "verify" {
		return releaseVerify(&call{Env: env, root: root}, args[2:])
	}
	if args[0] == "host" && len(args) >= 2 {
		verb, ok := hostVerbs[args[1]]
		if !ok {
			return usageError("host %s is not a verb", args[1])
		}
		return verb(ctx, &call{Env: env, root: root}, args[2:])
	}
	if args[0] != "run" || len(args) < 2 {
		return usageError("expected `run VERB`, `host VERB` or `release verify`, got %q", strings.Join(args, " "))
	}
	verb, ok := verbs[args[1]]
	if !ok {
		return usageError("run %s is not a verb", args[1])
	}
	return verb(ctx, &call{Env: env, root: root}, args[2:])
}

// call is one invocation: the process it runs in, the --root it was given and, once a verb
// opens one, its connection.
type call struct {
	Env
	root string
}

type verb func(ctx context.Context, c *call, args []string) error

var verbs = map[string]verb{
	"start": start, "list": list, "attach": attach, "get": get, "wait": wait, "steer": steer, "stop": stop, "update": update,
}

// open reaches the service, having decided what to do with what it finds there for a verb that only
// reads or steers, and does the version handshake.
func (c *call) open(ctx context.Context) (*client.Client, error) {
	return c.openFor(ctx, VerbRead)
}

// openFor is open for what the verb may do to the host.
func (c *call) openFor(ctx context.Context, verb Verb) (*client.Client, error) {
	dial, err := c.settle(ctx, verb)
	if err != nil {
		return nil, err
	}
	conn, err := within(ctx, c.Clock, handshakeBound, "shake hands", func(ctx context.Context) (*client.Client, error) {
		return client.Dial(ctx, client.Dialer(dial), client.Options{BinaryVersion: c.Version})
	})
	if err != nil {
		// A handshake that fails without a reason of the service's own means no service answered.
		classified := classify(err)
		if exitCodeOf(classified) == ExitFailed && ctx.Err() == nil && !errors.As(err, new(*client.IncompatibleError)) {
			return nil, unavailable("%v", err)
		}
		return nil, classified
	}
	return conn, nil
}

// flags makes the flag set of a verb. It reports its own errors through the returned error.
func flags(name string) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	return fs
}

// parse reads flags and positional arguments in any order, since `run steer TEXT --agent A` puts a
// flag after a positional. It re-parses what is left after each positional so a later flag is still
// found, which would otherwise let a "--" terminator's effect expire at the end of the fs.Parse call
// that consumed it: a later positional that itself starts with "-" (such as steer text) would then be
// misread as an unknown flag. So "--" is handled here, once, for the rest of args, however many
// positionals come before it.
func parse(fs *flag.FlagSet, args []string) ([]string, error) {
	var positional []string
	for {
		if len(args) > 0 && args[0] == "--" {
			return append(positional, args[1:]...), nil
		}
		if err := fs.Parse(args); err != nil {
			if errors.Is(err, flag.ErrHelp) {
				return nil, err
			}
			if strings.HasPrefix(fs.Name(), "release ") || strings.HasPrefix(fs.Name(), "host ") {
				return nil, usageError("%s: %v", fs.Name(), err)
			}
			return nil, usageError("run %s: %v", fs.Name(), err)
		}
		if fs.NArg() == 0 {
			return positional, nil
		}
		positional = append(positional, fs.Arg(0))
		args = fs.Args()[1:]
	}
}

// session is a connection with the stream of runs open and one run chosen.
type session struct {
	client *client.Client
	feed   *feed
	view   *View
	// history is what the snapshot showed of the run, oldest first.
	history []Change
	cancel  context.CancelFunc
}

// Close ends the stream and the connection.
func (s *session) Close() {
	s.cancel()
	_ = s.client.Close()
}

// join opens the service, reads the snapshot and picks the run named by arg, which may be empty, in
// which case it means the run resolve picks.
// The returned context ends the stream and must be used for every later call.
func (c *call) join(ctx context.Context, arg string) (*session, context.Context, error) {
	repo := ""
	if arg == "" {
		var err error
		if repo, err = c.repository(""); err != nil {
			return nil, nil, usageError("no RUN was given, and %v", err)
		}
	}
	conn, err := c.open(ctx)
	if err != nil {
		return nil, nil, err
	}
	ctx, cancel := context.WithCancel(ctx)
	s := &session{client: conn, cancel: cancel}
	f, snapshot, err := openFeed(ctx, c.Clock, conn)
	if err != nil {
		s.Close()
		return nil, nil, err
	}
	run, err := resolve(arg, repo, snapshot.GetRuns(), c.Canonical)
	if err != nil {
		s.Close()
		return nil, nil, err
	}
	s.feed, s.view = f, NewView(run.GetId())
	s.history = s.view.Load(snapshot)
	return s, ctx, nil
}
