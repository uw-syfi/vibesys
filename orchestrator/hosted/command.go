package hosted

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"

	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/mcp"
	"github.com/uw-syfi/vibesys/orchestrator/internal/production"
)

// Dial opens a connection to a host process that serves the orchestrator's operator service.
type Dial func(ctx context.Context) (net.Conn, error)

// HostProgram is the executable name used by host commands and remote connections.
const HostProgram = "vibesys-host"

// Usage lists the verbs Command runs, for the host binary's own usage text.
const Usage = `  mcp
  [--host HOST] run start --repository DIR --task NAME --brief TEXT [--harness claude_code|codex] [--fanout N] [--model M] [--effort E] [--deadline RFC3339]
  [--host HOST] run stop RUN
  [--host HOST] run watch [--after SEQUENCE]
  [--host HOST] send AGENT TEXT
`

// IsCommand says whether a verb is one of Command's.
func IsCommand(verb string) bool {
	return verb == "mcp" || verb == "run" || verb == "send" || verb == "--host"
}

// Command runs one orchestrator verb: the stdio tool server an agent launches (mcp), or the headless
// client (run, send). The client reaches this host through local, or HOST through remote(HOST).
// It returns the process exit code.
func Command(ctx context.Context, version string, args []string, local Dial, remote func(host string) Dial, stdin io.Reader, stdout, stderr io.Writer) int {
	fail := func(err error) int {
		fmt.Fprintf(stderr, "%s: %v\n", HostProgram, err)
		return 1
	}
	global := flag.NewFlagSet(HostProgram, flag.ContinueOnError)
	global.SetOutput(stderr)
	host := global.String("host", "", "run the verb on this host through ssh; empty means this host")
	if err := global.Parse(args); err != nil {
		return 2
	}
	rest := global.Args()
	if len(rest) == 0 {
		fmt.Fprint(stderr, "usage:\n"+Usage)
		return 2
	}
	if rest[0] == "mcp" {
		return runMCP(ctx, version, stdin, stdout, fail)
	}
	dial := local
	if *host != "" {
		dial = remote(*host)
	}
	c, err := client.Dial(ctx, client.Dialer(dial), client.Options{BinaryVersion: version})
	if err != nil {
		return fail(err)
	}
	defer c.Close()
	h := client.Headless{Client: c, IDs: production.CommandIDs{}, Out: stdout}
	if err := dispatchVerb(ctx, h, rest); err != nil {
		var usageErr usageError
		if errors.As(err, &usageErr) {
			fmt.Fprint(stderr, "usage:\n"+Usage)
		}
		return fail(err)
	}
	return 0
}

// runMCP serves the tools of the session this process was launched for. Its identity is read from
// the environment the orchestrator gave the agent, and nowhere else.
func runMCP(ctx context.Context, version string, stdin io.Reader, stdout io.Writer, fail func(error) int) int {
	need := func(name string) (string, error) {
		v := os.Getenv(name)
		if v == "" {
			return "", fmt.Errorf("%s is not set: run this tool server from the environment the orchestrator launched the agent with", name)
		}
		return v, nil
	}
	var env = map[string]string{}
	for _, name := range []string{daemon.EnvSocket, daemon.EnvAgent, daemon.EnvSession, daemon.EnvToken, daemon.EnvRun, daemon.EnvRole} {
		v, err := need(name)
		if err != nil {
			return fail(err)
		}
		env[name] = v
	}
	n, err := strconv.ParseUint(env[daemon.EnvSession], 10, 32)
	if err != nil {
		return fail(fmt.Errorf("%s=%q is not a session number", daemon.EnvSession, env[daemon.EnvSession]))
	}
	c, err := client.Dial(ctx, production.UnixDialer(env[daemon.EnvSocket]), client.Options{
		BinaryVersion: version,
		Session:       &client.Session{Agent: env[daemon.EnvAgent], Session: uint32(n), Token: env[daemon.EnvToken]},
	})
	if err != nil {
		return fail(err)
	}
	defer c.Close()
	server := mcp.New(mcp.Identity{
		Agent: core.AgentID(env[daemon.EnvAgent]), Run: core.RunID(env[daemon.EnvRun]), Role: core.AgentRole(env[daemon.EnvRole]),
	}, c, production.RandomHex(6))
	if err := server.Serve(ctx, stdin, stdout); err != nil {
		return fail(err)
	}
	return 0
}

type usageError struct{ error }

func dispatchVerb(ctx context.Context, h client.Headless, rest []string) error {
	switch {
	case rest[0] == "send":
		if len(rest) != 3 {
			return usageError{errors.New("send takes an agent id and the text")}
		}
		return h.Send(ctx, rest[1], rest[2])
	case len(rest) < 2:
		return usageError{errors.New("run needs start, stop or watch")}
	}
	fs := flag.NewFlagSet("run "+rest[1], flag.ContinueOnError)
	switch rest[1] {
	case "start":
		var a client.StartArgs
		fs.StringVar(&a.Repository, "repository", "", "the repository the run works in")
		fs.StringVar(&a.Task, "task", "", "the task to optimize")
		fs.StringVar(&a.Brief, "brief", "", "optional steering text for the lead, such as a note about cost or harnesses")
		fs.StringVar(&a.Harness, "harness", "claude_code", "claude_code or codex")
		fs.StringVar(&a.Model, "model", "", "the lead's model")
		fs.StringVar(&a.Effort, "effort", "", "the lead's reasoning effort")
		fs.StringVar(&a.Deadline, "deadline", "", "RFC 3339 time the run stops by")
		fanout := fs.Uint("fanout", 4, "how many subagents may be live at once")
		if err := fs.Parse(rest[2:]); err != nil {
			return usageError{err}
		}
		a.FanoutLimit = uint32(*fanout)
		return h.StartRun(ctx, a)
	case "stop":
		if err := fs.Parse(rest[2:]); err != nil || fs.NArg() != 1 {
			return usageError{errors.New("run stop takes a run id")}
		}
		return h.StopRun(ctx, fs.Arg(0))
	case "watch":
		after := fs.Uint64("after", 0, "continue after this sequence instead of starting with a snapshot")
		if err := fs.Parse(rest[2:]); err != nil {
			return usageError{err}
		}
		return h.Watch(ctx, *after)
	}
	return usageError{fmt.Errorf("run %s is not start, stop or watch", rest[1])}
}

// SSH reaches the vibesys-host on host through `ssh HOST vibesys-host connect`.
func SSH(host string) Dial {
	argv := sshCommand(host)
	return Dial(production.CommandDialer(argv...))
}

func sshCommand(host string) []string {
	return []string{"ssh", host, HostProgram, "connect"}
}
