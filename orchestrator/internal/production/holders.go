package production

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// HolderPlace is where one session's holder lives. The host names both places, and the holder is
// told them in its bootstrap.
type HolderPlace struct {
	Socket    string
	Directory string
}

// LockFile is the file a holder locks for its whole life.
func (p HolderPlace) LockFile() string { return filepath.Join(p.Directory, "holder.lock") }

// ProcessesFile is the SessionProcesses record a holder keeps while it holds the lock.
func (p HolderPlace) ProcessesFile() string { return filepath.Join(p.Directory, "processes.pb") }

// LogFile is where the holder's output goes. The host opens it and hands it over as the holder's
// standard output and error, so it is a file for a person to read: nothing decides by it.
func (p HolderPlace) LogFile() string { return filepath.Join(p.Directory, "holder.log") }

// Bootstrap is the message a holder at the place starts from.
func (p HolderPlace) Bootstrap() *pb.SessionBootstrap {
	return &pb.SessionBootstrap{ListenSocket: p.Socket, StateDirectory: p.Directory, LockFile: p.LockFile(), ProcessesFile: p.ProcessesFile()}
}

// ErrNoHolder says nothing answers at a place.
var ErrNoHolder = errors.New("nothing holds this session")

// SessionHolders is the world a session's holder lives in: the one interface between the host's start
// protocol and processes, sockets and files. ProcessHolders is the implementation; a test passes one
// it can crash at each call.
type SessionHolders interface {
	// Dial reaches what listens at a place, or returns ErrNoHolder when nothing does.
	Dial(ctx context.Context, place HolderPlace) (HolderLink, error)
	// Spawn starts a holder at a place, in a session of its own so it outlives this process. It
	// returns a channel that is closed when the process has ended. A holder's only input is its
	// bootstrap, so nothing is added to the words of command. It first ends what a gone holder left
	// at the place, and refuses with ErrHolderLives when a holder lives there.
	Spawn(place HolderPlace, command []string, bootstrap *pb.SessionBootstrap) (<-chan struct{}, error)
	// AwaitListening returns once something listens at a place. It returns an error when exited is
	// closed first, or when the context ends. With a nil exited, for a holder this host did not
	// start, it returns an error when that holder ends first.
	AwaitListening(ctx context.Context, place HolderPlace, exited <-chan struct{}) error
	// Alive says a holder lives at a place: it holds the place's lock, which the system frees when the
	// process ends, however it ends. This is the host's only proof of life, and a place with no lock
	// has no holder, whatever any file says.
	Alive(place HolderPlace) bool
	// End ends the holder at a place and its agents, and leaves nothing that names either.
	End(place HolderPlace) error
	// Reap ends what a holder that is gone left running at a place. A place a holder lives at is left
	// alone. A record of processes that cannot be read is reported as a *ProcessRecordError, and
	// nothing is ended for it.
	Reap(place HolderPlace) error
	// ReapAgent does Reap for every place of an agent's sessions that nothing holds, so no old agent
	// process works in a worktree under a new session.
	ReapAgent(places []HolderPlace) error
	// Worktree makes a detached git worktree of a repository at path, unless one is there.
	Worktree(ctx context.Context, repository, path string) error
}

// HolderLink is the host's side of AgentSessionService. The stream it opens never waits: a Next with
// nothing ready returns daemon.ErrIdle.
type HolderLink interface {
	Hello(ctx context.Context, request *pb.SessionHelloRequest) (*pb.SessionHelloResponse, error)
	Open(ctx context.Context, request *pb.OpenSessionRequest) (*pb.OpenSessionResponse, error)
	Send(ctx context.Context, request *pb.SendRequest) error
	// Follow streams a session's events after an offset. gone reads the error that ended a stream.
	Follow(ctx context.Context, after uint64, gone func(error) error) (daemon.EventStream, error)
	Close()
}

// ProcessHolders is SessionHolders over real processes, one Unix socket per session and gRPC.
type ProcessHolders struct {
	Log *slog.Logger
	// Signals ends processes. Nil ends them through the operating system.
	Signals ProcessSignals
	// Git runs git in a repository and returns its combined output. Nil runs the git program.
	Git func(ctx context.Context, repository string, args ...string) ([]byte, error)
}

func (h *ProcessHolders) git(ctx context.Context, repository string, args ...string) ([]byte, error) {
	if h.Git != nil {
		return h.Git(ctx, repository, args...)
	}
	return exec.CommandContext(ctx, "git", append([]string{"-C", repository}, args...)...).CombinedOutput()
}

func (h *ProcessHolders) signals() ProcessSignals {
	if h.Signals == nil {
		return OSSignals{}
	}
	return h.Signals
}

// Dial implements SessionHolders.
func (h *ProcessHolders) Dial(ctx context.Context, place HolderPlace) (HolderLink, error) {
	conn, err := (&net.Dialer{}).DialContext(ctx, "unix", place.Socket)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, ErrNoHolder
	}
	conn.Close()
	client, err := grpc.NewClient("unix://"+place.Socket, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	return &grpcLink{conn: client, client: pb.NewAgentSessionServiceClient(client), log: h.Log}, nil
}

// Spawn implements SessionHolders. The process is this one's child in a session of its own, so an
// early exit is seen at once and nothing of it stays if this process dies. Its output goes to a log
// this process opens, and its bootstrap to its standard input, which closes once written.
func (h *ProcessHolders) Spawn(place HolderPlace, command []string, bootstrap *pb.SessionBootstrap) (<-chan struct{}, error) {
	if err := os.MkdirAll(place.Directory, 0o700); err != nil {
		return nil, err
	}
	skipped, err := prepareStart(place, h.signals())
	h.said(place, skipped)
	if err != nil {
		return nil, err
	}
	data, err := proto.Marshal(bootstrap)
	if err != nil {
		return nil, err
	}
	log, err := os.OpenFile(place.LogFile(), os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return nil, err
	}
	defer log.Close()
	cmd := exec.Command(command[0], command[1:]...)
	cmd.Stdin = bytes.NewReader(data)
	cmd.Stdout, cmd.Stderr = log, log
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("%s: %w", strings.Join(command, " "), err)
	}
	exited := make(chan struct{})
	go func() {
		_ = cmd.Wait()
		close(exited)
	}()
	return exited, nil
}

// AwaitListening implements SessionHolders. A holder this host did not start has no exit to watch,
// so its lock stands for it: once that is free and nothing listens, it is gone.
func (h *ProcessHolders) AwaitListening(ctx context.Context, place HolderPlace, exited <-chan struct{}) error {
	adopted := exited == nil
	for {
		if h.listening(place) {
			return nil
		}
		if adopted && !alive(place) {
			return errors.New("it ended before it listened")
		}
		select {
		case <-exited:
			// A process of ours that ended at once because another holds the place: wait for that one.
			if !alive(place) {
				return errors.New("it exited at start")
			}
			adopted, exited = true, nil
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(50 * time.Millisecond):
		}
	}
}

func (h *ProcessHolders) listening(place HolderPlace) bool {
	conn, err := net.DialTimeout("unix", place.Socket, time.Second)
	if err != nil {
		return false
	}
	conn.Close()
	return true
}

// Alive implements SessionHolders.
func (h *ProcessHolders) Alive(place HolderPlace) bool { return alive(place) }

// End implements SessionHolders.
func (h *ProcessHolders) End(place HolderPlace) error {
	skipped, err := killHolder(place, h.signals())
	h.said(place, skipped)
	return err
}

// Reap implements SessionHolders.
func (h *ProcessHolders) Reap(place HolderPlace) error {
	skipped, err := reapUnheld(place, h.signals())
	h.said(place, skipped)
	return err
}

// LeaderStarted implements ProcessSignals. The start time is what `ps` prints for it, in a fixed
// locale and zone, and is only ever compared for equality, so no /proc is read and the same code
// serves Linux and macOS. ps exits with status 1 and prints nothing when no such process exists.
func (OSSignals) LeaderStarted(pid int) (string, bool, error) {
	cmd := exec.Command("/bin/ps", "-o", "lstart=", "-p", strconv.Itoa(pid))
	cmd.Env = []string{"LC_ALL=C", "TZ=UTC"}
	out, err := cmd.Output()
	started := strings.TrimSpace(string(out))
	var exit *exec.ExitError
	switch {
	case err == nil && started != "":
		return started, true, nil
	case err == nil, errors.As(err, &exit) && exit.ExitCode() == 1 && started == "":
		return "", false, nil
	default:
		return "", false, fmt.Errorf("ps -o lstart= -p %d: %w", pid, err)
	}
}

// said logs the process groups a look at a place left alone because they are not the agent's.
func (h *ProcessHolders) said(place HolderPlace, skipped []string) {
	if h.Log == nil {
		return
	}
	for _, s := range skipped {
		h.Log.Warn("left a process group alone", "session", place.Directory, "why", s)
	}
}

// ReapAgent implements SessionHolders.
func (h *ProcessHolders) ReapAgent(places []HolderPlace) error {
	var errs []error
	for _, place := range places {
		errs = append(errs, h.Reap(place))
	}
	return errors.Join(errs...)
}

// Worktree implements SessionHolders.
func (h *ProcessHolders) Worktree(ctx context.Context, repository, path string) error {
	if _, err := os.Stat(path); err == nil {
		return nil
	}
	out, err := h.git(ctx, repository, "worktree", "add", "--detach", path)
	if err != nil && lostItsRecord(out) {
		// A `git worktree prune` (this host's own, at the end of another run, or the user's) between
		// git making the record of the new worktree and locking it removes the record, and git then
		// fails to open a file in it. The other process is done by the time this one returns, so
		// one more try meets no record and makes it whole. Once: a second failure is a real one.
		out, err = h.git(ctx, repository, "worktree", "add", "--detach", path)
	}
	if err != nil {
		return fmt.Errorf("%w: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

// lostItsRecord says git's output is a worktree add that lost its record in .git/worktrees to
// another git process.
func lostItsRecord(out []byte) bool {
	text := string(out)
	return strings.Contains(text, "could not open") && strings.Contains(text, "/worktrees/")
}

// grpcLink is HolderLink over gRPC.
type grpcLink struct {
	conn   *grpc.ClientConn
	client pb.AgentSessionServiceClient
	log    *slog.Logger
}

func (l *grpcLink) Hello(ctx context.Context, request *pb.SessionHelloRequest) (*pb.SessionHelloResponse, error) {
	return l.client.Hello(ctx, request)
}

func (l *grpcLink) Open(ctx context.Context, request *pb.OpenSessionRequest) (*pb.OpenSessionResponse, error) {
	return l.client.Open(ctx, request)
}

func (l *grpcLink) Send(ctx context.Context, request *pb.SendRequest) error {
	_, err := l.client.Send(ctx, request)
	return err
}

func (l *grpcLink) Close() { _ = l.conn.Close() }

// Follow implements HolderLink. A goroutine reads the stream into a buffer, so Next never waits; the
// buffer is bounded, and a full one holds the stream back through flow control. Its context bounds
// the opening call only: the stream ends with Close.
func (l *grpcLink) Follow(_ context.Context, after uint64, gone func(error) error) (daemon.EventStream, error) {
	ctx, cancel := context.WithCancel(context.Background())
	stream, err := l.client.Follow(ctx, &pb.FollowRequest{AfterOffset: after})
	if err != nil {
		cancel()
		return nil, err
	}
	f := &followStream{events: make(chan *pb.SessionEvent, 256), cancel: cancel, gone: gone}
	go func() {
		for {
			resp, err := stream.Recv()
			if err != nil {
				l.log.Info("the session's stream ended", "err", err)
				f.end.Store(err)
				close(f.events)
				return
			}
			select {
			case f.events <- resp.GetEvent():
			case <-ctx.Done():
				f.end.Store(ctx.Err())
				close(f.events)
				return
			}
		}
	}()
	return f, nil
}

type followStream struct {
	events chan *pb.SessionEvent
	end    errorBox
	cancel context.CancelFunc
	gone   func(error) error
}

func (f *followStream) Next() (*pb.SessionEvent, error) {
	select {
	case ev, ok := <-f.events:
		if ok {
			return ev, nil
		}
		err := f.end.Load()
		if errors.Is(err, io.EOF) {
			err = errors.New("the session ended its stream")
		}
		return nil, f.gone(err)
	default:
		return nil, daemon.ErrIdle
	}
}

func (f *followStream) Close() { f.cancel() }

type errorBox struct {
	mu  sync.Mutex
	err error
}

func (b *errorBox) Store(err error) { b.mu.Lock(); b.err = err; b.mu.Unlock() }
func (b *errorBox) Load() error     { b.mu.Lock(); defer b.mu.Unlock(); return b.err }
