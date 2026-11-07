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
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"syscall"
	"time"

	"google.golang.org/grpc"

	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/jobs"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/privatesocket"
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

// Layout says where the daemon keeps its files and its agent-only socket. The operator reaches the
// daemon through the socket of the process that hosts it.
type Layout struct {
	// State is the directory proven local that holds the database and the token secret.
	State localfs.Dir
	// AgentSocket is the agent-only socket, the one every launched session is told. It serves only
	// what an agent may do, to callers that carry a session's credentials.
	AgentSocket string
}

// LayoutFor resolves the layout: the state directory chosen in the host's configuration, or fallback
// when none was. The agent socket is in the state directory,
// which outlives a login session: the runtime directory ($XDG_RUNTIME_DIR, /run/user/UID) is removed
// when the last login of a user ends on a machine without lingering, and a live run's tools would
// lose their socket with it. A state directory so long that the socket's path does not fit in a Unix
// socket address is an error that says what to do, never another place.
func LayoutFor(chosen, fallback string) (Layout, error) {
	state, err := localfs.StateDir(chosen, fallback, localfs.SystemDetector{})
	if err != nil {
		return Layout{}, err
	}
	agentSocket, err := state.AgentSocket()
	if err != nil {
		return Layout{}, err
	}
	return Layout{State: state, AgentSocket: agentSocket}, nil
}

// UnixDialer connects to a daemon's socket.
func UnixDialer(socket string) client.Dialer {
	return func(ctx context.Context) (net.Conn, error) {
		var d net.Dialer
		return d.DialContext(ctx, "unix", socket)
	}
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
	// Root is the root this process is the host of, which it records as the state directory's holder.
	Root string

	Sidecars daemon.Sidecars
	Jobs     jobs.JobSource
	// Tree is the file system a start is checked against.
	Tree Tree
	// Version is the binary's version, reported by Hello.
	Version string
	// JobTool is what the agents of a run that starts now use: the CLI from ResolveCLI and the daemon it
	// answers with. It is asked when a run starts, so a repaired install is used by the next run with
	// no restart.
	JobTool func(context.Context) (core.JobToolEndpoint, error)
	// Parts says whether a run can be started now.
	Parts PartsCheck
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
	if wiring.Tree == nil {
		return nil, errors.New("production: Wiring.Tree is required: starts are checked against it")
	}
	if wiring.Parts == nil {
		return nil, errors.New("production: Wiring.Parts is required: starts are checked against it")
	}
	db, err := store.OpenSQLite(layout.State, store.SQLiteOptions{})
	if err != nil {
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, ReadHolder(layout.State.Path())
		}
		return nil, fmt.Errorf("open the database: %w", err)
	}
	if err := writeHolder(layout.State.Path(), wiring.Version, wiring.Root); err != nil {
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
			Search: searchStore(wiring.Search), Store: db, Clock: SystemClock{}, Nonces: RandomNonces{}, Hub: h, Log: log,
			OnCommit: func() {
				for _, c := range []chan struct{}{x.wake, x.commits} {
					select {
					case c <- struct{}{}:
					default:
					}
				}
			},
		},
		Sidecars: wiring.Sidecars, Checkouts: checkoutsOf(wiring.Sidecars), Jobs: wiring.Jobs, Reconcile: reconcile(wiring.Search), JobLog: jobLog(wiring.Search), Authority: authority, Bounds: Timeouts{},
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
		Authority: x.authority, Audience: audience, Bounds: Timeouts{}, Binary: x.wiring.Version,
		Starts: StartChecks{Sidecars: x.wiring.Sidecars, Parts: x.wiring.Parts, Tree: x.wiring.Tree}, JobTool: x.wiring.JobTool,
	})
}

// LiveRuns counts the runs that have not reached their end.
func (x *Daemon) LiveRuns() uint32 {
	var n uint32
	for _, r := range x.d.State().Runs() {
		if !r.Status.Final() {
			n++
		}
	}
	return n
}

// Live reports whether any run has not reached its end.
func (x *Daemon) Live() bool { return x.LiveRuns() > 0 }

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
	if err := privatesocket.EnsureDirectory(path); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	listener, err := privatesocket.Listen(path)
	if err != nil {
		return nil, err
	}
	return listener, nil
}

// NoSidecars is the host of a build with no sidecar launcher: it launches nothing, and says why.
type NoSidecars struct{}

// Launch implements daemon.Sidecars.
func (NoSidecars) Launch(context.Context, daemon.SessionStart) (daemon.Sidecar, error) {
	return nil, errors.New("this build has no way to start agent sessions, so none can start")
}

// Attach implements daemon.Sidecars.
func (NoSidecars) Attach(context.Context, core.Session) (daemon.Sidecar, error) {
	return nil, daemon.ErrSidecarGone
}
