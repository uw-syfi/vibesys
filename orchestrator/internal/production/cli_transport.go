package production

import (
	"context"
	"net"

	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
)

// CLIUnixDialer opens the socket the tool answered.
type CLIUnixDialer struct{}

// Dial implements clidaemon.Dialer.
func (CLIUnixDialer) Dial(ctx context.Context, s clidaemon.Socket) (net.Conn, error) {
	return (&net.Dialer{}).DialContext(ctx, "unix", s.Path())
}

func connectCLI(_ context.Context, socket clidaemon.Socket) (clidaemon.Connection, error) {
	return clidaemon.NewClient(socket, CLIUnixDialer{})
}
