package daemon

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"

	"google.golang.org/grpc"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// ErrAlreadyRunning says another daemon of this user holds the instance lock.
var ErrAlreadyRunning = errors.New("a daemon is already running")

// Runtime is what the daemon runs on. A shipped binary and a test compose it differently; the
// daemon itself never branches on which.
type Runtime struct {
	Stores       app.Stores
	Supervisors  supervisor.Supervisors
	Environments environment.Environments
	Branches     gitrepo.Branches
}

// Config is what Run needs.
type Config struct {
	// Dir is the opened runtime directory: the socket and the locks are in it.
	Dir     runtimedir.Dir
	Version string
	Runtime Runtime
	Clock   clock.Clock
	// Logf receives the daemon's diagnostics. It may be nil.
	Logf func(format string, args ...any)
	// Ready receives one runtimedir.ReadyByte once the daemon serves, and is closed. A daemon that fails
	// first closes it without a byte, so the client that started it learns of the failure from
	// the pipe and not from a clock. It may be nil.
	Ready io.WriteCloser
}

// Run is the daemon process. It takes the instance lock, recovers from what the stores and the
// supervisors hold, serves the socket, and returns when ctx is done or a client calls Stop. It
// fails with ErrAlreadyRunning, touching nothing, when another daemon holds the lock.
//
// The socket is opened only after recovery, so a client that connects sees a daemon that has
// already decided what every job is. Jobs that run have supervisors of their own, so stopping
// the daemon stops no job.
func Run(ctx context.Context, cfg Config) (err error) {
	ready := cfg.Ready
	defer func() {
		if err != nil && ready != nil {
			ready.Close()
		}
	}()

	lock, err := runtimedir.TryLock(cfg.Dir.DaemonLock())
	switch {
	case errors.Is(err, runtimedir.ErrHeld):
		return ErrAlreadyRunning
	case err != nil:
		return fmt.Errorf("daemon: the instance lock: %w", err)
	}
	defer lock.Unlock()

	// With the lock held, a socket in the way belongs to a daemon that died.
	if err := os.Remove(cfg.Dir.Socket()); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("daemon: removing the old socket: %w", err)
	}

	a := app.New(app.Config{
		Clock:        cfg.Clock,
		Stores:       cfg.Runtime.Stores,
		Environments: cfg.Runtime.Environments,
		Supervisors:  cfg.Runtime.Supervisors,
		Branches:     cfg.Runtime.Branches,
		Logf:         cfg.Logf,
	})
	defer a.Close()
	if err := a.Start(ctx); err != nil {
		return fmt.Errorf("daemon: recovering: %w", err)
	}

	listener, err := net.Listen("unix", cfg.Dir.Socket())
	if err != nil {
		return fmt.Errorf("daemon: %w", err)
	}
	defer os.Remove(cfg.Dir.Socket())
	if err := os.Chmod(cfg.Dir.Socket(), 0o600); err != nil {
		listener.Close()
		return fmt.Errorf("daemon: %w", err)
	}

	srv := NewServer(a, cfg.Version)
	server := grpc.NewServer()
	cliv1.RegisterDaemonServiceServer(server, srv)
	served := make(chan error, 1)
	go func() { served <- server.Serve(listener) }()

	if ready != nil {
		if _, err := ready.Write([]byte{runtimedir.ReadyByte}); err != nil {
			server.Stop()
			return fmt.Errorf("daemon: telling the client it serves: %w", err)
		}
		ready.Close()
		ready = nil
	}

	select {
	case <-ctx.Done():
	case <-srv.Stopping():
	case err := <-served:
		return fmt.Errorf("daemon: serving: %w", err)
	}
	// Calls that wait on a job end now; then the answer to Stop reaches its caller before the
	// socket goes.
	srv.Shutdown()
	server.GracefulStop()
	return nil
}
