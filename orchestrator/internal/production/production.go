// Package production holds the real world the daemon and its clients run in: the system clock,
// random nonces, the Unix socket, the state directory, the process that runs over ssh, and the
// loop that serves. Nothing here decides anything; each piece is the one place its kind of I/O is
// allowed, so the packages that decide stay free of it and run in a simulation.
package production

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"

	"google.golang.org/grpc"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/jobs"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
)

// Version is the release version, or "dev".
var Version = "dev"

// SystemClock reads the system clock.
type SystemClock struct{}

// Now implements daemon.Clock.
func (SystemClock) Now() time.Time { return time.Now().UTC() }

// RandomNonces mints nonces from the system's random source.
type RandomNonces struct{}

// Next implements daemon.Nonces.
func (RandomNonces) Next() core.Nonce { return core.Nonce(RandomHex(12)) }

// RandomHex returns 2n random hex characters.
func RandomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		// The system's random source failing leaves no safe way to mint an id.
		panic(fmt.Sprintf("production: no random bytes: %v", err))
	}
	return hex.EncodeToString(b)
}

// CommandIDs mints the id of each command from the random source.
type CommandIDs struct{}

// NewCommandID implements client.IDs.
func (CommandIDs) NewCommandID() string { return "cli-" + RandomHex(12) }

// Layout says where the daemon keeps its files and its socket.
type Layout struct {
	// State is the directory proven local that holds the database and the token secret.
	State localfs.Dir
	// Socket is the daemon's private Unix socket.
	Socket string
}

// maxSocketPath is the longest path a Unix socket address holds on the systems that matter.
const maxSocketPath = 100

// LayoutFor resolves the layout from the environment. The socket goes under the user's runtime
// directory when there is one, and in the state directory otherwise.
func LayoutFor(getenv func(string) string, home string) (Layout, error) {
	state, err := localfs.StateDir(getenv, home, localfs.SystemDetector{})
	if err != nil {
		return Layout{}, err
	}
	socket := filepath.Join(state.Path(), "orchestrator.sock")
	if run := getenv("XDG_RUNTIME_DIR"); run != "" {
		socket = filepath.Join(run, "vibesys-orchestrator.sock")
	}
	if len(socket) > maxSocketPath {
		return Layout{}, fmt.Errorf("the socket path %s is longer than %d bytes, which a Unix socket cannot hold: set a shorter state directory", socket, maxSocketPath)
	}
	return Layout{State: state, Socket: socket}, nil
}

// UnixDialer connects to a daemon's socket.
func UnixDialer(socket string) client.Dialer {
	return func(ctx context.Context) (net.Conn, error) {
		var d net.Dialer
		return d.DialContext(ctx, "unix", socket)
	}
}

// StartingDialer connects to the daemon's socket, starting a daemon first if none answers. The
// daemon is crash-only, so starting one is all there is to it: it recovers and serves.
func StartingDialer(socket string, start func() error) client.Dialer {
	return func(ctx context.Context) (net.Conn, error) {
		conn, err := UnixDialer(socket)(ctx)
		if err == nil {
			return conn, nil
		}
		if !errors.Is(err, os.ErrNotExist) && !errors.Is(err, syscall.ECONNREFUSED) {
			return nil, err
		}
		if err := start(); err != nil {
			return nil, fmt.Errorf("no daemon answers on %s and starting one failed: %w", socket, err)
		}
		// The new daemon takes a moment to recover and listen: ask again a bounded number of times.
		for range 100 {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(50 * time.Millisecond):
			}
			if conn, err = UnixDialer(socket)(ctx); err == nil {
				return conn, nil
			}
		}
		return nil, fmt.Errorf("started a daemon, but nothing answers on %s after 5 seconds: read its log in the state directory", socket)
	}
}

// StartDetached runs this binary's daemon in its own session, so it outlives the connection that
// started it.
func StartDetached(layout Layout) func() error {
	return func() error {
		self, err := os.Executable()
		if err != nil {
			return err
		}
		if err := os.MkdirAll(layout.State.Path(), 0o700); err != nil {
			return err
		}
		log, err := os.OpenFile(filepath.Join(layout.State.Path(), "daemon.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
		if err != nil {
			return err
		}
		defer log.Close()
		cmd := exec.Command(self, "daemon")
		cmd.Stdout, cmd.Stderr = log, log
		cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
		if err := cmd.Start(); err != nil {
			return err
		}
		return cmd.Process.Release()
	}
}

// CommandDialer runs a command, such as `ssh host vibesys-orchestrator connect`, and makes its
// stdio a connection. The process ends with the connection.
func CommandDialer(argv ...string) client.Dialer {
	return func(context.Context) (net.Conn, error) {
		// The command outlives the dial's context, which ends once the connection is made.
		cmd := exec.Command(argv[0], argv[1:]...)
		stdin, err := cmd.StdinPipe()
		if err != nil {
			return nil, err
		}
		stdout, err := cmd.StdoutPipe()
		if err != nil {
			return nil, err
		}
		cmd.Stderr = os.Stderr
		if err := cmd.Start(); err != nil {
			return nil, fmt.Errorf("run %s: %w", argv[0], err)
		}
		return client.StdioConn(&waitCloser{ReadCloser: stdout, cmd: cmd}, stdin), nil
	}
}

type waitCloser struct {
	io.ReadCloser
	cmd *exec.Cmd
}

func (w *waitCloser) Close() error {
	_ = w.ReadCloser.Close()
	_ = w.cmd.Process.Kill()
	_ = w.cmd.Wait()
	return nil
}

// secret returns the token secret of a state directory, creating it on first use.
func secret(dir localfs.Dir) ([]byte, error) {
	path := filepath.Join(dir.Path(), "token-secret")
	if data, err := os.ReadFile(path); err == nil && len(data) >= 16 {
		return data, nil
	}
	data := make([]byte, 32)
	if _, err := rand.Read(data); err != nil {
		return nil, err
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return nil, fmt.Errorf("write the token secret: %w", err)
	}
	return data, nil
}

// Wiring is what a daemon needs that this build does not provide itself. The sidecar host is the
// sidecar item's, and the job source and the plan reader are vibesys-cli's; until they are wired
// the daemon starts and refuses launches with a message that says so.
type Wiring struct {
	Sidecars daemon.Sidecars
	Jobs     jobs.JobSource
	Plans    daemon.PlanReader
}

// Serve runs the daemon until ctx ends: it opens the database, listens on the private socket and
// runs the loop. There is no shutdown work; a stop is a crash and the next start recovers.
func Serve(ctx context.Context, layout Layout, wiring Wiring, log *slog.Logger) error {
	db, err := store.OpenSQLite(layout.State, store.SQLiteOptions{})
	if err != nil {
		return fmt.Errorf("open the database: %w", err)
	}
	defer db.Close()
	key, err := secret(layout.State)
	if err != nil {
		return err
	}
	authority, err := daemon.NewAuthority(key)
	if err != nil {
		return err
	}
	h := hub.New(hub.DefaultLimits)
	wake := make(chan struct{}, 1)
	d, err := daemon.Open(daemon.Config{
		Engine: daemon.EngineDeps{
			Store: db, Clock: SystemClock{}, Nonces: RandomNonces{}, Hub: h, Log: log,
			OnCommit: func() {
				select {
				case wake <- struct{}{}:
				default:
				}
			},
		},
		Sidecars: wiring.Sidecars, Jobs: wiring.Jobs, Authority: authority, Socket: layout.Socket,
		JobPollEvery: 5 * time.Second,
	})
	if err != nil {
		return err
	}
	if err := d.Reattach(ctx); err != nil {
		return fmt.Errorf("reattach the sidecars: %w", err)
	}
	listener, err := listenPrivate(layout.Socket)
	if err != nil {
		return err
	}
	defer os.Remove(layout.Socket)
	server := grpc.NewServer()
	daemon.NewService(d.Engine, wiring.Plans, authority, Version).Register(server)

	ticker := time.NewTicker(daemon.MergeWindow)
	defer ticker.Stop()
	wake <- struct{}{}
	loopDone := make(chan struct{})
	go func() {
		defer close(loopDone)
		d.Run(ctx, ticker.C, wake, log)
	}()
	go func() {
		<-ctx.Done()
		server.Stop()
	}()
	log.Info("orchestrator serving", "socket", layout.Socket, "version", Version)
	err = server.Serve(listener)
	<-ctx.Done()
	<-loopDone
	if err != nil && !errors.Is(err, grpc.ErrServerStopped) {
		return err
	}
	return nil
}

// listenPrivate listens on a Unix socket only the user can reach. The store already holds the
// state directory's lock, so no other daemon owns the path, and a file left there by a crash is
// stale.
func listenPrivate(path string) (net.Listener, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("remove the stale socket: %w", err)
	}
	old := syscall.Umask(0o177)
	defer syscall.Umask(old)
	l, err := net.Listen("unix", path)
	if err != nil {
		return nil, fmt.Errorf("listen on %s: %w", path, err)
	}
	return l, nil
}

// NoSidecars is the host of a build with no sidecar launcher: it launches nothing, and says why.
type NoSidecars struct{}

// Launch implements daemon.Sidecars.
func (NoSidecars) Launch(context.Context, core.LaunchSession, daemon.LaunchEnv) (daemon.Sidecar, error) {
	return nil, errors.New("this build has no sidecar launcher wired in, so no agent session can start")
}

// Attach implements daemon.Sidecars.
func (NoSidecars) Attach(context.Context, core.Session) (daemon.Sidecar, error) {
	return nil, daemon.ErrSidecarGone
}

// NoJobs is a job source with no jobs, for a build with no vibesys-cli wired in.
type NoJobs struct{}

// Jobs implements jobs.JobSource.
func (NoJobs) Jobs(context.Context, core.Run) ([]core.JobUpdate, error) { return nil, nil }

// NoPlans is a plan reader for a build with no vibesys-cli wired in.
type NoPlans struct{}

// ListPlans implements daemon.PlanReader.
func (NoPlans) ListPlans(context.Context, core.Run) ([]*pb.Plan, error) {
	return nil, errors.New("this build has no vibesys-cli wired in, so plans cannot be read")
}

// ViewPlan implements daemon.PlanReader.
func (NoPlans) ViewPlan(context.Context, core.Run, uint32) (*pb.Plan, error) {
	return nil, errors.New("this build has no vibesys-cli wired in, so plans cannot be read")
}
