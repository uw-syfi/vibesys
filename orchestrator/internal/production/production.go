// Package production holds the real world the daemon and its clients run in: the system clock,
// random nonces, the Unix socket, the state directory, the process that runs over ssh, and the
// loop that serves. Nothing here decides anything; each piece is the one place its kind of I/O is
// allowed, so the packages that decide stay free of it and run in a simulation.
package production

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
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

// Layout says where the daemon keeps its files and its agent-only socket. The operator reaches the
// daemon through the socket of the process that hosts it.
type Layout struct {
	// State is the directory proven local that holds the database and the token secret.
	State localfs.Dir
	// AgentSocket is the agent-only socket, the one every launched session is told. It serves only
	// what an agent may do, to callers that carry a session's credentials.
	AgentSocket string
}

// maxSocketPath is the longest path a Unix socket address holds on the systems that matter.
const maxSocketPath = 100

// LayoutFor resolves the layout from the environment. The agent socket goes under the user's
// runtime directory when there is one, and in the state directory otherwise. In the runtime
// directory its name carries a short hash of the state directory, so orchestrators with different
// state directories never share, or take over, one agent socket.
func LayoutFor(getenv func(string) string, home string) (Layout, error) {
	state, err := localfs.StateDir(getenv, home, localfs.SystemDetector{})
	if err != nil {
		return Layout{}, err
	}
	agentSocket := filepath.Join(state.Path(), "agent.sock")
	if runtimeDir := getenv("XDG_RUNTIME_DIR"); runtimeDir != "" {
		agentSocket = filepath.Join(runtimeDir, agentSocketName(state.Path()))
	}
	if len(agentSocket) > maxSocketPath {
		return Layout{}, fmt.Errorf("the socket path %s is longer than %d bytes, which a Unix socket cannot hold: set a shorter state directory", agentSocket, maxSocketPath)
	}
	return Layout{State: state, AgentSocket: agentSocket}, nil
}

// agentSocketName is the agent socket's name in the runtime directory for a state directory.
func agentSocketName(state string) string {
	sum := sha256.Sum256([]byte(filepath.Clean(state)))
	return "vibesys-orchestrator-" + hex.EncodeToString(sum[:8]) + ".sock"
}

// UnixDialer connects to a daemon's socket.
func UnixDialer(socket string) client.Dialer {
	return func(ctx context.Context) (net.Conn, error) {
		var d net.Dialer
		return d.DialContext(ctx, "unix", socket)
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

// Wiring is what a daemon needs that this build does not provide itself. The job source and the
// plan reader are vibesys-cli's; until they are wired the daemon refuses them with a message that
// says so. A nil Jobs means there is no job source, which the daemon treats as no source and not as
// a source with no jobs.
type Wiring struct {
	Search *Reconciler

	Sidecars daemon.Sidecars
	Jobs     jobs.JobSource
	Plans    daemon.PlanReader
	// Version is the binary's version, reported by Hello.
	Version string
	// Location is where the agents of every run this daemon starts find the vibesys-cli and its
	// daemon and store: the CLI from ResolveCLI, the rest from the daemon's own environment.
	Location core.CliLocation
}

// Daemon is an opened daemon: it holds the state directory's lock and its database, and serves the
// agent-only socket and runs its loop once Run is called. The operator's side is a Service that
// any gRPC server registers, so the daemon can share a server and a socket with other services.
type Daemon struct {
	layout    Layout
	wiring    Wiring
	log       *slog.Logger
	db        *store.SQLite
	authority daemon.Authority
	d         *daemon.Daemon
	wake      chan struct{}
	commits   chan struct{}
}

// Open takes the state directory's lock, opens its database and recovers. It fails with an error
// that wraps ErrHeld when another process holds the lock.
func Open(layout Layout, wiring Wiring, log *slog.Logger) (*Daemon, error) {
	db, err := store.OpenSQLite(layout.State, store.SQLiteOptions{})
	if err != nil {
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, fmt.Errorf("%w: %s", ErrHeld, ReadHolder(layout.State.Path()))
		}
		return nil, fmt.Errorf("open the database: %w", err)
	}
	if err := writeHolder(layout.State.Path(), wiring.Version); err != nil {
		db.Close()
		return nil, err
	}
	key, err := secret(layout.State)
	if err != nil {
		db.Close()
		return nil, err
	}
	authority, err := daemon.NewAuthority(key)
	if err != nil {
		db.Close()
		return nil, err
	}
	x := &Daemon{
		layout: layout, wiring: wiring, log: log, db: db, authority: authority,
		wake: make(chan struct{}, 1), commits: make(chan struct{}, 1),
	}
	h := hub.New(hub.DefaultLimits)
	x.d, err = daemon.Open(daemon.Config{
		Engine: daemon.EngineDeps{
			Search: searchStore(wiring.Search), Store: db, Clock: SystemClock{}, Nonces: RandomNonces{}, Hub: h, Log: log, Location: wiring.Location,
			OnCommit: func() {
				for _, c := range []chan struct{}{x.wake, x.commits} {
					select {
					case c <- struct{}{}:
					default:
					}
				}
			},
		},
		Sidecars: wiring.Sidecars, Jobs: wiring.Jobs, Reconcile: reconcile(wiring.Search), Authority: authority, Bounds: Timeouts{},
		AgentSocket: layout.AgentSocket, JobPollEvery: 5 * time.Second,
	})
	if err != nil {
		db.Close()
		return nil, err
	}
	return x, nil
}

// Service is the daemon's OrchestratorService for one audience.
func (x *Daemon) Service(audience daemon.Audience) (*daemon.Service, error) {
	return daemon.NewService(x.d.Engine, daemon.ServiceDeps{
		Plans: x.wiring.Plans, Authority: x.authority, Audience: audience, Bounds: Timeouts{}, Binary: x.wiring.Version,
	})
}

// Live reports whether any run has not reached its end.
func (x *Daemon) Live() bool {
	for _, r := range x.d.State().Runs() {
		if !r.Status.Final() {
			return true
		}
	}
	return false
}

// Commits wakes after commits, coalesced, so a watcher of Live can look again.
func (x *Daemon) Commits() <-chan struct{} { return x.commits }

// Close releases the database and the state directory's lock.
func (x *Daemon) Close() error { return x.db.Close() }

// Run serves the agent-only socket and runs the loop until ctx ends or either fails. There is no
// shutdown work; a stop is a crash and the next start recovers.
func (x *Daemon) Run(ctx context.Context) error {
	// Reattach reports the calls it could not finish and leaves them to the loop, so a sidecar host
	// that is slow to answer does not keep the daemon from serving.
	if err := x.d.Reattach(ctx); err != nil {
		x.log.Warn("some sessions are not reattached yet; the loop retries", "err", err)
	}
	ctx, stopAll := context.WithCancel(ctx)
	defer stopAll()
	if x.wiring.Search != nil {
		defer x.wiring.Search.Close()
	}
	listener, err := listenPrivate(x.layout.AgentSocket)
	if err != nil {
		return err
	}
	defer os.Remove(x.layout.AgentSocket)
	service, err := x.Service(daemon.AudienceAgent)
	if err != nil {
		return err
	}
	server := grpc.NewServer()
	service.Register(server)
	failed := make(chan error, 1)
	go func() { failed <- server.Serve(listener) }()
	ticker := time.NewTicker(daemon.MergeWindow)
	defer ticker.Stop()
	x.wake <- struct{}{}
	loopDone := make(chan struct{})
	go func() {
		defer close(loopDone)
		x.d.Run(ctx, ticker.C, x.wake, x.log)
	}()
	x.log.Info("orchestrator serving", "state", x.layout.State.Path(), "agent_socket", x.layout.AgentSocket, "version", x.wiring.Version)
	serveErr := awaitServers(ctx, failed)
	stopAll()
	server.Stop()
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

// GatewayRoot is the host .vibesys directory a gateway serves from and its clients dial, so that a
// gateway belongs to exactly one orchestrator state directory. In order:
//
//  1. The --root flag, when given.
//  2. A subdirectory of the state directory when VIBESYS_ORCHESTRATOR_STATE_DIR is set, so a
//     client whose state is elsewhere never reaches a gateway (and the orchestrator inside it) that
//     serves another state, and never starts one under the default root. A relative setting is an
//     error naming it.
//  3. ~/.vibesys.
//
// The gateway's socket and the host's run files live under the root; the orchestrator's own state
// stays in the state directory, and a root derived from it is a separate subdirectory.
func GatewayRoot(flag string, getenv func(string) string, home string) (string, error) {
	switch state := getenv(localfs.EnvStateDir); {
	case flag != "":
		return filepath.Abs(flag)
	case state != "":
		if !filepath.IsAbs(state) {
			return "", fmt.Errorf("%s=%q must be an absolute path", localfs.EnvStateDir, state)
		}
		return filepath.Join(filepath.Clean(state), derivedRootName), nil
	case home == "":
		return "", errors.New("no home directory for the default root ~/.vibesys; pass --root or set " + localfs.EnvStateDir)
	default:
		return filepath.Join(home, ".vibesys"), nil
	}
}

// derivedRootName is the subdirectory of a state directory that is the gateway root derived from it.
const derivedRootName = "host"
