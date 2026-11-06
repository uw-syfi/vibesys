package hostmain

import (
	"context"
	"fmt"
	"log/slog"
	"os"

	"github.com/uw-syfi/vibesys/gateway/internal/buildinfo"
	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	schema "github.com/uw-syfi/vibesys/gateway/schemahash"
	"github.com/uw-syfi/vibesys/orchestrator/gatewayroot"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
	"github.com/uw-syfi/vibesys/orchestrator/hosted"
)

// exitCode is a command's exit status, carried out of run as an error.
type exitCode int

func (c exitCode) Error() string { return fmt.Sprintf("exit status %d", int(c)) }

// startOrchestrator opens the orchestrator for this host. stop ends the host, and is what
// HostControlService calls when a client asks it to stop.
func startOrchestrator(ctx context.Context, stop func(), root string, config *hostconfig.Config, getenv func(string) string, home string) (*hosted.Orchestrator, error) {
	binary, err := os.Executable()
	if err != nil {
		return nil, err
	}
	return hosted.Start(ctx, hosted.Options{
		Version: buildinfo.Version, Executable: binary, Log: slog.New(slog.NewTextHandler(os.Stderr, nil)),
		Root: root, Config: config, Getenv: getenv, Home: home, APISchemaHash: schema.APISchemaHash, Stop: stop,
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

// gatewayRoot is the root every verb of this process dials or serves, by gatewayroot.Rule.
func gatewayRoot(flag string) (string, error) {
	home, _ := os.UserHomeDir()
	binary, err := os.Executable()
	if err != nil {
		return "", err
	}
	return gatewayroot.For(flag, os.Getenv, home, gatewayroot.Installed(binary))
}
