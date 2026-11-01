package host

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net"
	"os"
	"os/exec"
	"strconv"
	"sync"
	"syscall"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor/rpc"
)

// Host is supervisor.Supervisors over processes. Create it with New.
type Host struct {
	root          string
	command       func(runDir string) *exec.Cmd
	binaryVersion string

	mu       sync.Mutex
	released map[lifecycle.Ref]bool
}

var _ supervisor.Supervisors = (*Host)(nil)

// New returns a Host that keeps run directories under root, which must exist, be private and be
// on local disk, and starts a supervisor on a directory with the command command returns for it.
// The command must run Serve on the directory, with the readiness pipe on file descriptor 3.
func New(root string, command func(runDir string) *exec.Cmd, binaryVersion string) *Host {
	return &Host{root: root, command: command, binaryVersion: binaryVersion, released: map[lifecycle.Ref]bool{}}
}

// ReadyFD is the file descriptor a supervisor process finds its readiness pipe on.
const ReadyFD = 3

// Spawn implements supervisor.Supervisors.
func (h *Host) Spawn(ctx context.Context, launch lifecycle.Launch) (supervisor.Handle, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	ref := launch.Ref()
	dir := runDir(h.root + string(os.PathSeparator) + dirName(ref))
	if err := checkSocketPath(dir); err != nil {
		return nil, err
	}
	if err := os.Mkdir(string(dir), 0o700); err != nil {
		if errors.Is(err, fs.ErrExist) {
			return nil, &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("job %s already has a run", ref)}
		}
		return nil, fmt.Errorf("creating the run directory: %w", err)
	}
	handle, err := h.spawn(ctx, dir, launch)
	if err != nil {
		os.RemoveAll(string(dir))
		return nil, err
	}
	return handle, nil
}

func (h *Host) spawn(ctx context.Context, dir runDir, launch lifecycle.Launch) (supervisor.Handle, error) {
	if err := writeLaunch(dir, launch); err != nil {
		return nil, fmt.Errorf("writing the launch: %w", err)
	}
	errFile, err := os.OpenFile(dir.stderr(), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return nil, err
	}
	defer errFile.Close()
	readyR, readyW, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	defer readyR.Close()

	cmd := h.command(string(dir))
	cmd.Stdin, cmd.Stdout, cmd.Stderr = nil, errFile, errFile
	cmd.ExtraFiles = append([]*os.File{readyW}, cmd.ExtraFiles...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		readyW.Close()
		return nil, fmt.Errorf("starting the supervisor: %w", err)
	}
	readyW.Close()
	pid := cmd.Process.Pid
	exited := make(chan struct{})
	// The daemon is the supervisor's parent while it lives, so it reaps it. A later daemon is
	// not the parent, and the supervisor is then reaped by init.
	go func() { cmd.Wait(); close(exited) }()
	killGroup := func() {
		syscall.Kill(-pid, syscall.SIGKILL)
		<-exited
	}

	ready := make(chan error, 1)
	go func() {
		var b [1]byte
		_, err := io.ReadFull(readyR, b[:])
		ready <- err
	}()
	select {
	case err := <-ready:
		if err != nil {
			killGroup()
			return nil, fmt.Errorf("the supervisor did not start: %s", h.why(dir, err))
		}
	case <-ctx.Done():
		killGroup()
		return nil, ctx.Err()
	}
	handle, err := h.connect(ctx, dir, launch.Ref())
	if err != nil {
		killGroup()
		return nil, err
	}
	return handle, nil
}

// why says why a supervisor process ended before it served, from what it wrote to stderr.
func (h *Host) why(dir runDir, err error) string {
	data, _ := os.ReadFile(dir.stderr())
	if text := string(bytes.TrimSpace(data)); text != "" {
		return text
	}
	return fmt.Sprintf("it exited without saying why (%v)", err)
}

// handle is a supervisor.Handle that also closes its connection when the supervisor is released.
type handle struct {
	*rpc.Client
	host *Host
	ref  lifecycle.Ref
	conn *grpc.ClientConn
}

func (h *handle) Release(ctx context.Context) error {
	err := h.Client.Release(ctx)
	if err == nil || errors.Is(err, supervisor.ErrGone) {
		h.host.mu.Lock()
		h.host.released[h.ref] = true
		h.host.mu.Unlock()
		h.conn.Close()
	}
	return err
}

// connect dials the run directory's socket and runs the handshake.
func (h *Host) connect(ctx context.Context, dir runDir, ref lifecycle.Ref) (*handle, error) {
	conn, err := grpc.NewClient("passthrough:///supervisor",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "unix", dir.socket())
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	client, err := rpc.Connect(ctx, conn, h.binaryVersion)
	if err != nil {
		conn.Close()
		return nil, err
	}
	return &handle{Client: client, host: h, ref: ref, conn: conn}, nil
}

// Runs implements supervisor.Supervisors.
func (h *Host) Runs(ctx context.Context) ([]supervisor.Run, error) {
	entries, err := os.ReadDir(h.root)
	if err != nil {
		return nil, err
	}
	var out []supervisor.Run
	for _, e := range entries {
		ref, ok := refOf(e.Name())
		if !ok || !e.IsDir() {
			continue
		}
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		dir := runDir(h.root + string(os.PathSeparator) + e.Name())
		hd, err := h.connect(ctx, dir, ref)
		if err != nil {
			// A supervisor that does not answer, or answers in a way this build cannot use, is
			// not one the daemon can follow.
			out = append(out, supervisor.Run{Ref: ref, Presence: lifecycle.PresenceGone})
			continue
		}
		out = append(out, supervisor.Run{Ref: ref, Presence: lifecycle.PresenceAlive, Handle: hd})
	}
	return out, nil
}

// PID returns the process id of the supervisor of a job, which the supervisor wrote down when it
// came up. It is for diagnostics and for a test that kills a supervisor; the supervisor can be
// gone, and the id is then stale.
func (h *Host) PID(ref lifecycle.Ref) (int, error) {
	data, err := os.ReadFile(runDir(h.root + string(os.PathSeparator) + dirName(ref)).pid())
	if err != nil {
		return 0, err
	}
	return strconv.Atoi(string(bytes.TrimSpace(data)))
}

// Forget implements supervisor.Supervisors.
func (h *Host) Forget(ctx context.Context, ref lifecycle.Ref) error {
	dir := runDir(h.root + string(os.PathSeparator) + dirName(ref))
	if _, err := os.Stat(string(dir)); errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	h.mu.Lock()
	released := h.released[ref]
	h.mu.Unlock()
	if !released {
		if hd, err := h.connect(ctx, dir, ref); err == nil {
			hd.conn.Close()
			return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("job %s still has a supervisor", ref)}
		}
	}
	if err := os.RemoveAll(string(dir)); err != nil {
		return err
	}
	h.mu.Lock()
	delete(h.released, ref)
	h.mu.Unlock()
	return nil
}

// Info describes one run directory under a root, for a tool or a test that needs to find a
// supervisor from outside the daemon.
type Info struct {
	Ref lifecycle.Ref
	// PID is the supervisor's process id as it wrote it down. The supervisor can be gone, and the
	// id is then stale.
	PID int
	// Socket is the path the supervisor listens on.
	Socket string
}

// Inspect lists the run directories under root. A directory whose supervisor has not written its
// id yet is listed with PID zero.
func Inspect(root string) ([]Info, error) {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, err
	}
	var out []Info
	for _, e := range entries {
		ref, ok := refOf(e.Name())
		if !ok || !e.IsDir() {
			continue
		}
		dir := runDir(root + string(os.PathSeparator) + e.Name())
		info := Info{Ref: ref, Socket: dir.socket()}
		if data, err := os.ReadFile(dir.pid()); err == nil {
			info.PID, _ = strconv.Atoi(string(bytes.TrimSpace(data)))
		}
		out = append(out, info)
	}
	return out, nil
}
