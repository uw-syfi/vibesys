package hosted

import (
	"context"
	"fmt"
	"io"
	"os"
	"strconv"

	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/mcp"
	"github.com/uw-syfi/vibesys/orchestrator/internal/production"
)

// HostProgram is the executable name used by host commands and remote connections.
const HostProgram = "vibesys-host"

// Usage lists the verbs Command runs, for the host binary's own usage text. The verbs that start,
// follow and stop runs are vibesys-client's (orchestrator/clientcli).
const Usage = "  mcp\n"

// IsCommand says whether a verb is one of Command's.
func IsCommand(verb string) bool { return verb == "mcp" }

// Command runs the stdio tool server an agent launches (mcp). It returns the process exit code.
func Command(ctx context.Context, version string, args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) != 1 || !IsCommand(args[0]) {
		fmt.Fprint(stderr, "usage:\n"+Usage)
		return 2
	}
	return runMCP(ctx, version, stdin, stdout, func(err error) int {
		fmt.Fprintf(stderr, "%s: %v\n", HostProgram, err)
		return 1
	})
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
