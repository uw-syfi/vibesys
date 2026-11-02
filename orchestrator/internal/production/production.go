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

// CallBound is how long the daemon waits for one call to a sidecar, the job source or the plan
// reader before it gives up and retries.
const CallBound = 30 * time.Second

// Timeouts bounds each call by CallBound on the system clock.
type Timeouts struct{}

// Bound implements daemon.Bounds.
func (Timeouts) Bound(ctx context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(ctx, CallBound)
}

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
	// Socket is the operator's private Unix socket. No agent's environment names it.
	Socket string
	// AgentSocket is the agent-only socket, the one every launched session is told. It serves only
	// what an agent may do, to callers that carry a session's credentials.
	AgentSocket string
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
	socket, agentSocket := socketPaths(state.Path(), getenv("XDG_RUNTIME_DIR"))
	for _, path := range []string{socket, agentSocket} {
		if len(path) > maxSocketPath {
			return Layout{}, fmt.Errorf("the socket path %s is longer than %d bytes, which a Unix socket cannot hold: set a shorter state directory", path, maxSocketPath)
		}
	}
	return Layout{State: state, Socket: socket, AgentSocket: agentSocket}, nil
}

// socketPaths places the operator's socket and the agent-only socket, side by side: under the
// user's runtime directory when there is one, and in the state directory otherwise.
func socketPaths(state, runtimeDir string) (operator, agent string) {
	if runtimeDir != "" {
		return filepath.Join(runtimeDir, "vibesys-orchestrator.sock"), filepath.Join(runtimeDir, "vibesys-orchestrator-agent.sock")
	}
	return filepath.Join(state, "orchestrator.sock"), filepath.Join(state, "orchestrator-agent.sock")
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

// secret returns the token secret of a state directory, creating it on first use. It is written to
// a file of its own and renamed into place, so that a crash leaves either no secret or a whole one:
// a half-written secret would be regenerated on the next start and would end every live session's
// token. A file that exists and is too short is an error and is never replaced.
func secret(dir localfs.Dir) ([]byte, error) { return secretIn(dir.Path()) }

func secretIn(dir string) ([]byte, error) {
	path := filepath.Join(dir, "token-secret")
	data, err := os.ReadFile(path)
	switch {
	case err == nil && len(data) >= 16:
		return data, nil
	case err == nil:
		return nil, fmt.Errorf("%s holds %d bytes and needs at least 16: delete it to start over, which ends the tokens of every live session", path, len(data))
	case !errors.Is(err, os.ErrNotExist):
		return nil, fmt.Errorf("read the token secret: %w", err)
	}
	data = make([]byte, 32)
	if _, err := rand.Read(data); err != nil {
		return nil, err
	}
	tmp, err := os.CreateTemp(dir, "token-secret-*.tmp")
	if err != nil {
		return nil, fmt.Errorf("write the token secret: %w", err)
	}
	defer os.Remove(tmp.Name())
	_, werr := tmp.Write(data)
	if serr := tmp.Sync(); werr == nil {
		werr = serr
	}
	if cerr := tmp.Close(); werr == nil {
		werr = cerr
	}
	if werr == nil {
		werr = os.Chmod(tmp.Name(), 0o600)
	}
	if werr == nil {
		werr = os.Rename(tmp.Name(), path)
	}
	if werr != nil {
		return nil, fmt.Errorf("write the token secret: %w", werr)
	}
	return data, nil
}

// Wiring is what a daemon needs that this build does not provide itself. The sidecar host is the
// sidecar item's, and the job source and the plan reader are vibesys-cli's; until they are wired
// the daemon starts and refuses launches with a message that says so. A nil Jobs means there is no
// job source, which the daemon treats as no source and not as a source with no jobs.
type Wiring struct {
	Sidecars daemon.Sidecars
	Jobs     jobs.JobSource
	Plans    daemon.PlanReader
}

// Serve runs the daemon until ctx ends: it opens the database, listens on the operator's socket
// and on the agent-only socket, and runs the loop. There is no shutdown work; a stop is a crash and
// the next start recovers. If either server or the loop fails, the daemon stops with that error, so
// it never stays up serving nothing.
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
		Sidecars: wiring.Sidecars, Jobs: wiring.Jobs, Authority: authority, Bounds: Timeouts{},
		AgentSocket: layout.AgentSocket, JobPollEvery: 5 * time.Second,
	})
	if err != nil {
		return err
	}
	// Reattach reports the calls it could not finish and leaves them to the loop, so a sidecar host
	// that is slow to answer does not keep the daemon from serving.
	if err := d.Reattach(ctx); err != nil {
		log.Warn("some sessions are not reattached yet; the loop retries", "err", err)
	}
	ctx, stopAll := context.WithCancel(ctx)
	defer stopAll()
	var servers []*grpc.Server
	failed := make(chan error, 2)
	for _, s := range []struct {
		socket   string
		audience daemon.Audience
	}{{layout.Socket, daemon.AudienceOperator}, {layout.AgentSocket, daemon.AudienceAgent}} {
		listener, err := listenPrivate(s.socket)
		if err != nil {
			return err
		}
		defer os.Remove(s.socket)
		service, err := daemon.NewService(d.Engine, daemon.ServiceDeps{
			Plans: wiring.Plans, Authority: authority, Audience: s.audience, Bounds: Timeouts{}, Binary: Version,
		})
		if err != nil {
			return err
		}
		server := grpc.NewServer()
		service.Register(server)
		servers = append(servers, server)
		go func() { failed <- server.Serve(listener) }()
	}
	ticker := time.NewTicker(daemon.MergeWindow)
	defer ticker.Stop()
	wake <- struct{}{}
	loopDone := make(chan struct{})
	go func() {
		defer close(loopDone)
		d.Run(ctx, ticker.C, wake, log)
	}()
	log.Info("orchestrator serving", "socket", layout.Socket, "agent_socket", layout.AgentSocket, "version", Version)
	serveErr := awaitServers(ctx, failed)
	stopAll()
	for _, server := range servers {
		server.Stop()
	}
	<-loopDone
	if serveErr != nil && !errors.Is(serveErr, grpc.ErrServerStopped) {
		return serveErr
	}
	return nil
}

// awaitServers blocks until the context ends, which is a clean stop, or until a server stops, which
// is a failure whatever it returned: a server that returns nil has stopped serving too, and a
// daemon that stays up with a socket nobody answers is worse than one that exits.
func awaitServers(ctx context.Context, failed <-chan error) error {
	select {
	case <-ctx.Done():
		return nil
	case err := <-failed:
		if err == nil {
			return errors.New("a server stopped serving")
		}
		return err
	}
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
