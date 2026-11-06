package hostmain

import (
	"context"
	"fmt"
	"log/slog"
	"os"

	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	"github.com/uw-syfi/vibesys/orchestrator/gatewayroot"
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

// gatewayRoot is the root every verb of this process dials or serves: the --root flag, else the one
// derived from the orchestrator state directory, else ~/.vibesys (gatewayroot.For).
func gatewayRoot(flag string) (string, error) {
	getenv, home := hosted.Environ()
	return gatewayroot.For(flag, getenv, home)
}
