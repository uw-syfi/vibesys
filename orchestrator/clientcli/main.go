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
            [--model M] [--effort E] [--deadline RFC3339] [--detach] [--json]
  run list [--json]
  run attach [RUN]
  run get [RUN] [--json]
  run wait [RUN] --until started|report|progress|stalled|ended --timeout DURATION [--after N] [--json]
  run steer [RUN] TEXT [--agent AGENT] [--json]
  run stop [RUN] [--yes]
  host status [--json]
  host stop [--if-no-live-runs] [--force]
  host restart
  host configure [--state-directory DIR] [--session-holder CMD] [--skills-directory DIR]
                 [--job-tool PATH] [--tool-path DIR:DIR...] [--clear]
  release verify [--installed] DIR
  --version

RUN is a run id, a unique prefix of one, or omitted for the one live run in this git repository.
host status and host stop never start a host; host restart does. A host with live runs is not restarted.
host stop --force ends the process that holds the host's lock, for a host that does not answer.
host configure records what the next host starts with, and with no flag prints it.
release verify checks the release in DIR against its manifest, prints its version, and needs no host.
wait --until report returns for a final report and --until progress for any report. Both count the
reports posted after the wait began (reports are numbered from 1; run get --json shows the numbers),
and --after N counts those numbered above N instead; for a run that has already ended, with no
--after, the report it already has counts. --until stalled returns when the run stands still: its
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
	"start": start, "list": list, "attach": attach, "get": get, "wait": wait, "steer": steer, "stop": stop,
}

// open reaches the service, having decided what to do with what it finds there for a verb that only
// reads or steers, and does the version handshake.
func (c *call) open(ctx context.Context) (*client.Client, error) {
	return c.openFor(ctx, NeedService)
}

// openFor is open for what the verb needs of the host.
func (c *call) openFor(ctx context.Context, need Need) (*client.Client, error) {
	dial, err := c.settle(ctx, need)
	if err != nil {
		return nil, err
	}
	conn, err := client.Dial(ctx, client.Dialer(dial), client.Options{BinaryVersion: c.Version})
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
// flag after a positional.
func parse(fs *flag.FlagSet, args []string) ([]string, error) {
	var positional []string
	for {
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

// join opens the service, reads the snapshot and picks the run named by arg, which may be empty.
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
	f, snapshot, err := openFeed(ctx, conn)
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
