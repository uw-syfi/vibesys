package hosted

import (
	"context"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/mcp"
	"github.com/uw-syfi/vibesys/orchestrator/internal/production"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
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
	return runMCP(ctx, version, stdin, stdout, stderr, func(err error) int {
		fmt.Fprintf(stderr, "%s: %v\n", HostProgram, err)
		return 1
	})
}

// runMCP serves the tools of the session this process was launched for. Its identity is read from
// the environment the orchestrator gave the agent, and nowhere else.
func runMCP(ctx context.Context, version string, stdin io.Reader, stdout, stderr io.Writer, fail func(error) int) int {
	credentials, unknown, err := credentialsFrom(os.Environ())
	if err != nil {
		return fail(fmt.Errorf("run this tool server from the environment the orchestrator launched the agent with: %w", err))
	}
	if len(unknown) > 0 {
		// Reported and not refused: the environment is inherited, and a name in the prefix can be
		// another program's setting.
		fmt.Fprintf(stderr, "%s: ignoring %s, which the tool server does not read\n", HostProgram, strings.Join(unknown, ", "))
	}
	role, err := wire.RoleIn(credentials.GetRole())
	if err != nil {
		return fail(err)
	}
	c, err := client.Dial(ctx, production.UnixDialer(credentials.GetEndpoint()), client.Options{
		BinaryVersion: version,
		Session:       &client.Session{Agent: credentials.GetAgent(), Session: credentials.GetSession(), Token: credentials.GetToken()},
	})
	if err != nil {
		return fail(err)
	}
	defer c.Close()
	server := mcp.New(mcp.Identity{Agent: core.AgentID(credentials.GetAgent()), Run: core.RunID(credentials.GetRun()), Role: role}, c, production.RandomHex(6))
	if err := server.Serve(ctx, stdin, stdout); err != nil {
		return fail(err)
	}
	return 0
}

// credentialsFrom reads the tool server's credentials from an environment in the form os.Environ
// returns it. Every variable is required, and one that breaks its rule is refused naming its
// variable. It also returns the names in the credentials' namespace that no field reads.
func credentialsFrom(environ []string) (*pb.ToolServerCredentials, []string, error) {
	credentials := &pb.ToolServerCredentials{}
	if err := envcodec.Decode(envcodec.FromEnviron(environ), credentials); err != nil {
		return nil, nil, err
	}
	return credentials, envcodec.Unknown(environ, credentials), nil
}
