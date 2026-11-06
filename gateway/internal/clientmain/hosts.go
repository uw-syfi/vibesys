package clientmain

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"syscall"
	"time"

	"github.com/uw-syfi/vibesys/gateway/internal/connect"
	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	schema "github.com/uw-syfi/vibesys/gateway/schemahash"
	"github.com/uw-syfi/vibesys/orchestrator/clientcli"
	"github.com/uw-syfi/vibesys/orchestrator/gatewayroot"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostlock"
)

// terminateGrace is how long a host has to exit after it is asked to, before it is killed.
const terminateGrace = 10 * time.Second

// locator says how to reach the host for a --root value without starting one. The Dial it returns
// fails with clientcli.ErrNoHost when nobody serves.
func locator(root string) (func(ctx context.Context) (net.Conn, error), error) {
	home, _ := os.UserHomeDir()
	directory, err := gatewayroot.For(root, os.Getenv, home)
	if err != nil {
		return nil, err
	}
	socket := lifecycle.SocketPath(directory, schema.APISchemaHash)
	if err := lifecycle.ValidateSocketPath(socket); err != nil {
		return nil, err
	}
	return func(ctx context.Context) (net.Conn, error) {
		conn, err := (&net.Dialer{}).DialContext(ctx, "unix", socket)
		if connect.Absent(err) {
			return nil, fmt.Errorf("%w: %v", clientcli.ErrNoHost, err)
		}
		return conn, err
	}, nil
}

// hostProcesses is the machine's host processes, found through the lock each holds.
type hostProcesses struct{}

// Holder implements clientcli.HostProcesses.
func (hostProcesses) Holder(root string) (*pb.HostLock, error) {
	directory, err := hostRoot(root)
	if err != nil {
		return nil, err
	}
	record, held, err := lifecycle.Holder(directory, schema.APISchemaHash)
	if err != nil || !held {
		return nil, err
	}
	lock, err := hostlock.Parse(record)
	if err != nil {
		return nil, fmt.Errorf("a host holds %s and did not say who it is (%v); it predates host control, so end it by hand", directory, err)
	}
	return lock, nil
}

// Terminate implements clientcli.HostProcesses. It asks the process to exit, and kills it when it
// has not in terminateGrace. It looks at the lock again first, and signals nothing when the lock is
// free: the number in a record that nothing holds may belong to another program by now.
func (h hostProcesses) Terminate(ctx context.Context, root string, holder *pb.HostLock) error {
	current, err := h.Holder(root)
	if err != nil {
		return err
	}
	if current == nil || current.GetPid() != holder.GetPid() {
		return nil
	}
	pid := int(holder.GetPid())
	for _, signal := range []syscall.Signal{syscall.SIGTERM, syscall.SIGKILL} {
		if err := syscall.Kill(pid, signal); err != nil && !errors.Is(err, syscall.ESRCH) {
			return err
		}
		if h.exited(ctx, root, holder) {
			return nil
		}
	}
	return fmt.Errorf("process %d still holds the host lock", pid)
}

// exited waits, up to terminateGrace, for the holder to let go of the lock.
func (h hostProcesses) exited(ctx context.Context, root string, holder *pb.HostLock) bool {
	ctx, cancel := context.WithTimeout(ctx, terminateGrace)
	defer cancel()
	tick := time.NewTicker(50 * time.Millisecond)
	defer tick.Stop()
	for {
		if current, err := h.Holder(root); err == nil && (current == nil || current.GetPid() != holder.GetPid()) {
			return true
		}
		select {
		case <-ctx.Done():
			return false
		case <-tick.C:
		}
	}
}

func hostRoot(root string) (string, error) {
	home, _ := os.UserHomeDir()
	return gatewayroot.For(root, os.Getenv, home)
}
