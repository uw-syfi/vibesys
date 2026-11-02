package main

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"time"

	"github.com/uw-syfi/vibesys/gateway/internal/connect"
	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	schema "github.com/uw-syfi/vibesys/gateway/schemahash"
	"github.com/uw-syfi/vibesys/orchestrator/hosted"
)

// exitCode is a command's exit status, carried out of run as an error.
type exitCode int

func (c exitCode) Error() string { return fmt.Sprintf("exit status %d", int(c)) }

func startOrchestrator(ctx context.Context) (*hosted.Orchestrator, error) {
	binary, err := os.Executable()
	if err != nil {
		return nil, err
	}
	getenv, home := hosted.Environ()
	return hosted.Start(ctx, hosted.Options{
		Version: version, Executable: binary, Log: slog.New(slog.NewTextHandler(os.Stderr, nil)),
		Getenv: getenv, Home: home,
	}), nil
}

// holdWhileLive keeps the gateway from exiting for idleness while a run is live, by counting the
// live stretch as one open connection.
func holdWhileLive(ctx context.Context, live <-chan bool, monitor *lifecycle.Monitor) {
	held := false
	defer func() {
		if held {
			monitor.Closed()
		}
	}()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-live:
			switch {
			case now && !held:
				held = monitor.Opened()
			case !now && held:
				monitor.Closed()
				held = false
			}
		}
	}
}

// orchestratorVerb says whether args name an orchestrator verb, after an optional leading
// `--root PATH` that names the gateway the client verbs reach.
func orchestratorVerb(args []string) (bool, string, []string) {
	root := ""
	if len(args) >= 2 && args[0] == "--root" {
		root, args = args[1], args[2:]
	}
	if len(args) == 0 || !hosted.IsCommand(args[0]) {
		return false, "", nil
	}
	return true, root, args
}

// orchestratorCommand runs mcp, run or send. The client verbs reach this host's gateway through its
// socket, starting a gateway when none serves, as connect does; with --host they run
// `ssh HOST vibesysd connect`, so the remote vibesysd must be on that host's PATH.
func orchestratorCommand(ctx context.Context, root string, args []string) int {
	if root == "" {
		home, _ := os.UserHomeDir()
		root = filepath.Join(home, ".vibesys")
	}
	local := func(ctx context.Context) (net.Conn, error) {
		binary, err := os.Executable()
		if err != nil {
			return nil, err
		}
		startup, cancel := context.WithTimeout(ctx, 10*time.Second)
		defer cancel()
		return connect.Dial(startup, lifecycle.SocketPath(root, schema.APISchemaHash), binary, []string{"serve", "--root", root}, os.Stderr)
	}
	return hosted.Command(ctx, version, args, local, hosted.SSH, os.Stdin, os.Stdout, os.Stderr)
}
