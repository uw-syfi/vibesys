// Command vibesys-orchestrator is the orchestrator's one binary: the daemon, the stdio pipe that
// reaches it from another host, the tool server an agent uses, and the headless client.
//
//	vibesys-orchestrator daemon
//	vibesys-orchestrator connect
//	vibesys-orchestrator mcp
//	vibesys-orchestrator [--host HOST] run start|stop|watch ...
//	vibesys-orchestrator [--host HOST] send AGENT TEXT
//
// The client verbs reach the daemon on this host by its socket, or on HOST through
// `ssh HOST vibesys-orchestrator connect`. There is no other transport.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"syscall"

	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/mcp"
	"github.com/uw-syfi/vibesys/orchestrator/internal/production"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := run(ctx, os.Args[1:], os.Stdin, os.Stdout, os.Stderr)
	stop()
	os.Exit(code)
}

const usage = `usage:
  vibesys-orchestrator daemon
  vibesys-orchestrator connect
  vibesys-orchestrator mcp
  vibesys-orchestrator [--host HOST] run start --repository DIR --task NAME --brief TEXT [--harness claude_code|codex] [--fanout N] [--model M] [--effort E] [--deadline RFC3339]
  vibesys-orchestrator [--host HOST] run stop RUN
  vibesys-orchestrator [--host HOST] run watch [--after SEQUENCE]
  vibesys-orchestrator [--host HOST] send AGENT TEXT
`

func run(ctx context.Context, args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	fail := func(err error) int {
		fmt.Fprintf(stderr, "vibesys-orchestrator: %v\n", err)
		return 1
	}
	global := flag.NewFlagSet("vibesys-orchestrator", flag.ContinueOnError)
	global.SetOutput(stderr)
	host := global.String("host", "", "run the verb on this host through ssh; empty means this host")
	if err := global.Parse(args); err != nil {
		return 2
	}
	rest := global.Args()
	if len(rest) == 0 {
		fmt.Fprint(stderr, usage)
		return 2
	}
	home, _ := os.UserHomeDir()
	layout, layoutErr := production.LayoutFor(os.Getenv, home)

	switch rest[0] {
	case "daemon":
		if layoutErr != nil {
			return fail(layoutErr)
		}
		log := slog.New(slog.NewTextHandler(stderr, nil))
		err := production.Serve(ctx, layout, production.Wiring{
			Sidecars: production.NoSidecars{}, Plans: production.NoPlans{},
		}, log)
		if err != nil {
			return fail(err)
		}
		return 0
	case "connect":
		if layoutErr != nil {
			return fail(layoutErr)
		}
		dial := production.StartingDialer(layout.Socket, production.StartDetached(layout))
		if err := client.Connect(ctx, dial, stdin, stdout); err != nil {
			return fail(err)
		}
		return 0
	case "mcp":
		return runMCP(ctx, stdin, stdout, fail)
	case "run", "send":
		return runClient(ctx, *host, layout, layoutErr, rest, stdout, fail)
	}
	fmt.Fprint(stderr, usage)
	return 2
}

// runMCP serves the tools of the session this process was launched for. Its identity is read from
// the environment the orchestrator gave the agent, and nowhere else.
func runMCP(ctx context.Context, stdin io.Reader, stdout io.Writer, fail func(error) int) int {
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
		BinaryVersion: production.Version,
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

func runClient(ctx context.Context, host string, layout production.Layout, layoutErr error, rest []string, stdout io.Writer, fail func(error) int) int {
	var dial client.Dialer
	if host != "" {
		dial = production.CommandDialer("ssh", host, "vibesys-orchestrator", "connect")
	} else {
		if layoutErr != nil {
			return fail(layoutErr)
		}
		dial = production.StartingDialer(layout.Socket, production.StartDetached(layout))
	}
	c, err := client.Dial(ctx, dial, client.Options{BinaryVersion: production.Version})
	if err != nil {
		return fail(err)
	}
	defer c.Close()
	h := client.Headless{Client: c, IDs: production.CommandIDs{}, Out: stdout}
	if err := dispatchVerb(ctx, h, rest); err != nil {
		var usageErr usageError
		if errors.As(err, &usageErr) {
			fmt.Fprint(os.Stderr, usage)
		}
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
		fs.StringVar(&a.Brief, "brief", "", "the lead's first prompt")
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
