package production

import (
	"context"
	"net"
	"os"
	"syscall"

	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
)

// CLIFS inspects the daemon directory without following its final symlink.
type CLIFS struct{}

func (CLIFS) LstatDirectory(path string) (clidaemon.Directory, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return clidaemon.Directory{}, err
	}
	fs, err := (localfs.SystemDetector{}).Detect(path)
	if err != nil {
		return clidaemon.Directory{}, err
	}
	return clidaemon.Directory{Mode: info.Mode(), UID: int(info.Sys().(*syscall.Stat_t).Uid), Network: fs.Class != localfs.Local}, nil
}

// UnixDialer opens only sockets proven safe by Locate.
type CLIUnixDialer struct{}

func (CLIUnixDialer) Dial(ctx context.Context, s clidaemon.Socket) (net.Conn, error) {
	return (&net.Dialer{}).DialContext(ctx, "unix", s.Path())
}
func connectCLI(ctx context.Context, loc clidaemon.Location) (clidaemon.Connection, error) {
	s, err := clidaemon.Locate(loc, os.Getuid(), CLIFS{})
	if err != nil {
		return nil, err
	}
	return clidaemon.NewClient(s, CLIUnixDialer{})
}
