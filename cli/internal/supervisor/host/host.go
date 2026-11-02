package host

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"maps"
	"net"
	"os"
	"os/exec"
	"slices"
	"strconv"
	"sync"
	"syscall"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/owned"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor/rpc"
)

// Host is supervisor.Supervisors over processes. Create it with New.
type Host struct {
	root          string
	records       string
	bootID        func() (string, error)
	command       func(runDir string) *exec.Cmd
	binaryVersion string
	timers        clock.Timers
	clock         clock.Clock

	mu       sync.Mutex
	released map[lifecycle.Ref]bool
}

var _ supervisor.Supervisors = (*Host)(nil)

// Config is what a Host needs. Every field is required.
type Config struct {
	// Root keeps the run directories. It must exist, be private and be on local disk. It may go
	// away while the machine stays up, as a runtime directory does at logout.
	Root string
	// Records keeps a record of each run, outside Root and on a disk that outlives it, which is what
	// tells a run that went away with the machine from a run whose directory was removed.
	Records string
	// BootID names this boot of the machine.
	BootID func() (string, error)
	// Command is how a supervisor starts on a directory. It must run Serve on the directory, with
	// the readiness pipe on file descriptor 3.
	Command func(runDir string) *exec.Cmd
	// BinaryVersion is this build's version.
	BinaryVersion string
	// Timers bound how long a supervisor gets to answer a probe.
	Timers clock.Timers
	// Clock stamps the mark a run's record gets when its supervisor is told to start.
	Clock clock.Clock
}

// New returns a Host.
func New(cfg Config) *Host {
	return &Host{
		root: cfg.Root, records: cfg.Records, bootID: cfg.BootID, command: cfg.Command, binaryVersion: cfg.BinaryVersion,
		timers: cfg.Timers, clock: cfg.Clock, released: map[lifecycle.Ref]bool{},
	}
}

// probeTimeout is how long a supervisor gets to answer a probe, so that one that hangs cannot hold
// the daemon's start. It is not how death is told: that is the lock.
const probeTimeout = 10 * time.Second

func (h *Host) dir(ref lifecycle.Ref) runDir {
	return runDir(h.root + string(os.PathSeparator) + dirName(ref))
}

// ReadyFD is the file descriptor a supervisor process finds its readiness pipe on.
const ReadyFD = 3

// Spawn implements supervisor.Supervisors.
func (h *Host) Spawn(ctx context.Context, launch lifecycle.Launch) (supervisor.Handle, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	ref := launch.Ref()
	dir := h.dir(ref)
	if err := checkSocketPath(dir); err != nil {
		return nil, err
	}
	if err := h.writeRecord(ref, launch); err != nil {
		return nil, fmt.Errorf("recording the run: %w", err)
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
		// The directory is gone with whatever the attempt started, so the record is too.
		h.removeRecord(ref)
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
	// not the parent, and the supervisor is then reaped by init. It ends when the supervisor does,
	// which is after the daemon, so nothing can join it.
	owned.Detached(func() { cmd.Wait(); close(exited) })
	killGroup := func() {
		syscall.Kill(-pid, syscall.SIGKILL)
		<-exited
	}

	ready := make(chan error, 1)
	var reader owned.Group
	// Every way out with the pipe still open kills the group, which ends the read.
	defer reader.Wait()
	reader.Go(func() {
		var b [1]byte
		_, err := io.ReadFull(readyR, b[:])
		ready <- err
	})
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

// handle is a supervisor.Handle that closes its connection when the supervisor is released, and
// that says a supervisor is gone only with the proof. A connection can break for other reasons,
// and the process can be on its way out with its command still ending.
type handle struct {
	*rpc.Client
	host *Host
	ref  lifecycle.Ref
	dir  runDir
	conn *grpc.ClientConn
}

var _ supervisor.Handle = (*handle)(nil)

// proved turns ErrGone into the proof of it, or into a failure when there is none.
func (h *handle) proved(err error) error {
	if !errors.Is(err, supervisor.ErrGone) {
		return err
	}
	h.host.mu.Lock()
	released := h.host.released[h.ref]
	h.host.mu.Unlock()
	if released {
		// A supervisor that was let go is exiting, and its lock says nothing while it does.
		return supervisor.Gone{Proof: lifecycle.ProofReleased}
	}
	lock, lockErr := proveDead(h.dir)
	if lockErr != nil {
		return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("job %s: its supervisor does not answer and may still live", h.ref)}
	}
	lock.Unlock()
	return supervisor.Gone{Proof: lifecycle.ProofLockFree, Start: h.host.startOf(h.ref)}
}

// Start implements supervisor.Handle. The mark goes into the run's record first: a supervisor runs
// nothing before it is told, so a record without the mark proves the command never started.
func (h *handle) Start(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if err := h.host.markStartSent(h.ref); err != nil {
		return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("job %s: marking its start: %v", h.ref, err)}
	}
	return h.proved(h.Client.Start(ctx))
}

// Cancel implements supervisor.Handle.
func (h *handle) Cancel(ctx context.Context, reason string) error {
	return h.proved(h.Client.Cancel(ctx, reason))
}

// Follow implements supervisor.Handle.
func (h *handle) Follow(ctx context.Context, offset uint64, emit func([]byte) error) (lifecycle.Outcome, error) {
	out, err := h.Client.Follow(ctx, offset, emit)
	return out, h.proved(err)
}

// Release implements supervisor.Handle.
func (h *handle) Release(ctx context.Context) error {
	err := h.proved(h.Client.Release(ctx))
	if err == nil || errors.Is(err, supervisor.ErrGone) {
		h.host.mu.Lock()
		h.host.released[h.ref] = true
		h.host.mu.Unlock()
		h.Close()
	}
	return err
}

// Close implements supervisor.Handle. Closing a connection twice does nothing the second time.
func (h *handle) Close() { h.conn.Close() }

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
	return &handle{Client: client, host: h, ref: ref, dir: dir, conn: conn}, nil
}

// Runs implements supervisor.Supervisors. A run with a record and no directory is listed too.
func (h *Host) Runs(ctx context.Context) ([]supervisor.Run, error) {
	entries, err := os.ReadDir(h.root)
	if err != nil {
		return nil, err
	}
	found := map[lifecycle.Ref]bool{}
	for _, e := range entries {
		if ref, ok := refOf(e.Name()); ok && e.IsDir() {
			found[ref] = true
		}
	}
	recorded, err := h.recordedRefs()
	if err != nil {
		return nil, err
	}
	for _, ref := range recorded {
		found[ref] = true
	}
	refs := slices.Collect(maps.Keys(found))
	// Together, so that supervisors that do not answer cost one probe's time and not one each,
	// which a caller's deadline would turn into a failure to list any of them.
	out := make([]supervisor.Run, len(refs))
	errs := make([]error, len(refs))
	var probes owned.Group
	for i, ref := range refs {
		probes.Go(func() { out[i], errs[i] = h.Probe(ctx, ref) })
	}
	probes.Wait()
	if err := errors.Join(errs...); err != nil {
		for _, r := range out {
			if alive, ok := r.(supervisor.Alive); ok {
				alive.Handle.Close()
			}
		}
		return nil, err
	}
	return out, nil
}

// Probe implements supervisor.Supervisors. A supervisor that answers is alive and a free lock
// proves it dead. A run with no directory is dead only if the machine restarted since it began;
// with the same boot the directory was taken away from under what may still run. Anything else,
// such as one of another build, is a supervisor that may live, whose command may be running, and is
// never reported as gone.
func (h *Host) Probe(ctx context.Context, ref lifecycle.Ref) (supervisor.Run, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	dir := h.dir(ref)
	if _, err := os.Stat(string(dir)); errors.Is(err, fs.ErrNotExist) {
		return h.noDirectory(ref)
	}
	launch := readableLaunch(dir, ref)
	bounded, done := clock.Deadline(ctx, h.timers, probeTimeout)
	defer done()
	hd, err := h.connect(bounded, dir, ref)
	if err == nil {
		if launch == nil {
			hd.Close()
			return supervisor.Unknown{Ref: ref}, nil
		}
		return supervisor.Alive{Ref: ref, Handle: hd, Launch: *launch, Phase: hd.Phase()}, nil
	}
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	lock, err := proveDead(dir)
	switch {
	case errors.Is(err, runtimedir.ErrHeld):
		return supervisor.Unknown{Ref: ref, Launch: launch}, nil
	case err != nil:
		return nil, err
	}
	lock.Unlock()
	return supervisor.Dead{Ref: ref, Proof: lifecycle.ProofLockFree, Start: h.startOf(ref), Launch: launch}, nil
}

// noDirectory judges a run with no directory by its record.
func (h *Host) noDirectory(ref lifecycle.Ref) (supervisor.Run, error) {
	rec, ok, err := h.readRecord(ref)
	switch {
	case err != nil:
		// A record that cannot be read proves nothing.
		return supervisor.Unknown{Ref: ref}, nil
	case !ok:
		return supervisor.None{Ref: ref}, nil
	}
	boot, err := h.bootID()
	if err != nil {
		return nil, fmt.Errorf("reading the boot id: %w", err)
	}
	if rec.BootID != boot {
		start := lifecycle.Start(lifecycle.NeverStarted{})
		if rec.StartSent {
			start = lifecycle.MayHaveStarted{}
		}
		return supervisor.Dead{Ref: ref, Proof: lifecycle.ProofRebooted, Start: start, Launch: &rec.Launch}, nil
	}
	return supervisor.Unknown{Ref: ref, Launch: &rec.Launch}, nil
}

func readableLaunch(dir runDir, ref lifecycle.Ref) *lifecycle.Launch {
	launch, err := readLaunch(dir)
	if err != nil || launch.Ref() != ref {
		return nil
	}
	return &launch
}

// PID returns the process id of the supervisor of a job, which the supervisor wrote down when it
// came up. It is for diagnostics and for a test that kills a supervisor; the supervisor can be
// gone, and the id is then stale.
func (h *Host) PID(ref lifecycle.Ref) (int, error) {
	data, err := os.ReadFile(h.dir(ref).pid())
	if err != nil {
		return 0, err
	}
	return strconv.Atoi(string(bytes.TrimSpace(data)))
}

// Forget implements supervisor.Supervisors. The lock is held until the directory is gone, so a
// supervisor that was started and had not come up finds nothing to come up in. A released
// supervisor is exiting, and its lock says nothing while it does.
func (h *Host) Forget(ctx context.Context, ref lifecycle.Ref, proof lifecycle.DeathProof) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if !proof.Valid() {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("job %s: nothing proves its supervisor gone", ref)}
	}
	dir := h.dir(ref)
	if _, err := os.Stat(string(dir)); err == nil && proof != lifecycle.ProofReleased {
		lock, err := proveDead(dir)
		if errors.Is(err, runtimedir.ErrHeld) {
			return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("job %s still has a supervisor", ref)}
		}
		if err != nil {
			return err
		}
		defer lock.Unlock()
	}
	if err := os.RemoveAll(string(dir)); err != nil {
		return err
	}
	if err := h.removeRecord(ref); err != nil {
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
	// Alive is the file the supervisor holds a lock on for as long as it lives, so taking that
	// lock is seeing it gone.
	Alive string
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
		info := Info{Ref: ref, Socket: dir.socket(), Alive: dir.alive()}
		if data, err := os.ReadFile(dir.pid()); err == nil {
			info.PID, _ = strconv.Atoi(string(bytes.TrimSpace(data)))
		}
		out = append(out, info)
	}
	return out, nil
}
