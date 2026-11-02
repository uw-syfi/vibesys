package host

import (
	"context"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"time"

	"google.golang.org/grpc"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/owned"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor/rpc"
)

// readyByte is what a supervisor writes to its readiness pipe when it serves.
const readyByte = 'R'

// Deps is what a supervisor process runs jobs with.
type Deps struct {
	Environments  environment.Environments
	Checkouts     supervisor.Checkouts
	BinaryVersion string
	// Timers bound how long a released supervisor waits for its callers. It defaults to the
	// system's.
	Timers clock.Timers
}

// releaseGrace is how long a released supervisor waits for its calls to end. The answer to Release
// needs moments; a caller that stopped reading a stream would otherwise keep the process for good.
const releaseGrace = 5 * time.Second

// Serve is the supervisor process. It reads the launch in the run directory, serves the
// supervisor on the directory's socket, tells the daemon through ready that it does, and returns
// when the daemon releases it or ctx is done. Either way the command it runs is ended before it
// returns, so nothing outlives the process.
//
// ready receives one byte once the socket is accepting, and is closed. A supervisor that fails
// before that closes it without a byte, so the daemon learns of the failure from the pipe and
// not from a clock.
func Serve(ctx context.Context, dirPath string, deps Deps, ready io.WriteCloser) (err error) {
	dir := runDir(dirPath)
	defer func() {
		if err != nil {
			// A supervisor that failed before it served says why in the file the daemon reads.
			ready.Close()
		}
	}()
	// First of all, so that the daemon can never prove this process dead while it runs. A daemon
	// clearing the run holds this lock until the directory is gone, and the steps below then fail.
	alive, err := runtimedir.WaitLock(ctx, dir.alive())
	if err != nil {
		return fmt.Errorf("supervisor: %w", err)
	}
	defer alive.Unlock()
	if err := os.WriteFile(dir.pid(), []byte(strconv.Itoa(os.Getpid())), 0o600); err != nil {
		return fmt.Errorf("supervisor: %w", err)
	}
	launch, err := readLaunch(dir)
	if err != nil {
		return fmt.Errorf("supervisor: %w", err)
	}
	spool, err := openSpool(dir.spool())
	if err != nil {
		return fmt.Errorf("supervisor: %w", err)
	}
	defer spool.close()

	sup := supervisor.New(supervisor.Config{
		Launch: launch, Environments: deps.Environments, Checkouts: deps.Checkouts, Spool: spool,
	})
	defer sup.Close()

	listener, err := net.Listen("unix", dir.socket())
	if err != nil {
		return fmt.Errorf("supervisor: %w", err)
	}
	defer os.Remove(dir.socket())
	if err := os.Chmod(dir.socket(), 0o600); err != nil {
		listener.Close()
		return fmt.Errorf("supervisor: %w", err)
	}
	server := grpc.NewServer()
	cliv1.RegisterSupervisorServiceServer(server, rpc.NewServer(sup, deps.BinaryVersion))
	served := make(chan error, 1)
	var serving owned.Group
	defer serving.Wait()
	serving.Go(func() { served <- server.Serve(listener) })

	if _, err := ready.Write([]byte{readyByte}); err != nil {
		server.Stop()
		return fmt.Errorf("supervisor: telling the daemon it serves: %w", err)
	}
	ready.Close()

	select {
	case <-sup.Released():
		// Let the answer to Release reach the daemon before the socket goes.
		timers := deps.Timers
		if timers == nil {
			timers = clock.System{}
		}
		clock.Graceful(timers, releaseGrace, server.GracefulStop, server.Stop)
		os.Remove(dir.spool())
		return nil
	case <-ctx.Done():
		server.Stop()
		return nil
	case err := <-served:
		return fmt.Errorf("supervisor: serving: %w", err)
	}
}
