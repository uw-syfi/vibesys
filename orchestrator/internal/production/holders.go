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
	// bootstrap, so nothing is added to the words of command.
	Spawn(place HolderPlace, command []string, bootstrap *pb.SessionBootstrap) (<-chan struct{}, error)
	// AwaitListening returns once something listens at a place. It returns an error when exited is
	// closed first, or when the context ends.
	AwaitListening(ctx context.Context, place HolderPlace, exited <-chan struct{}) error
	// Listening says something listens at a place. A process that ended leaves no listener, whatever
	// its files say, so this is the host's proof of life.
	Listening(place HolderPlace) bool
	// End ends the holder at a place and its agents, and leaves nothing that names either.
	End(place HolderPlace) error
	// Reap ends what a holder that is gone left running at a place.
	Reap(place HolderPlace)
	// ReapAgent does Reap for every session of an agent that nothing holds, so no old agent process
	// works in a worktree under a new session.
	ReapAgent(agentDirectory string)
	// Worktree makes a detached git worktree of a repository at path, unless one is there.
	Worktree(ctx context.Context, repository, path string) error
	// Diagnostics is text for a person about why a holder at a place did not come up. It is display
	// only: no decision reads it.
	Diagnostics(place HolderPlace) string
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
}

const (
	holderLog     = "sidecar.log"
	holderPidFile = "sidecar.pid"
	holderGroups  = "agent.pgids"
)

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
	for _, stale := range []string{filepath.Join(place.Directory, holderPidFile), place.Socket} {
		_ = os.Remove(stale)
	}
	data, err := proto.Marshal(bootstrap)
	if err != nil {
		return nil, err
	}
	log, err := os.OpenFile(filepath.Join(place.Directory, holderLog), os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
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

// AwaitListening implements SessionHolders.
func (h *ProcessHolders) AwaitListening(ctx context.Context, place HolderPlace, exited <-chan struct{}) error {
	for {
		if h.Listening(place) {
			return nil
		}
		select {
		case <-exited:
			return errors.New("it exited at start")
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(50 * time.Millisecond):
		}
	}
}

// Listening implements SessionHolders.
func (h *ProcessHolders) Listening(place HolderPlace) bool {
	conn, err := net.DialTimeout("unix", place.Socket, time.Second)
	if err != nil {
		return false
	}
	conn.Close()
	return true
}

// End implements SessionHolders. The pid file is trusted only while the socket answers: the holder
// wrote it before it listened, so a pid the system has since given to another program is never
// signalled.
func (h *ProcessHolders) End(place HolderPlace) error {
	if h.Listening(place) {
		if pid, ok := readPid(place.Directory); ok {
			if err := syscall.Kill(pid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
				return err
			}
		}
	}
	reap(place.Directory)
	retire(place.Directory)
	return nil
}

// Reap implements SessionHolders.
func (h *ProcessHolders) Reap(place HolderPlace) { reap(place.Directory) }

// ReapAgent implements SessionHolders.
func (h *ProcessHolders) ReapAgent(agentDirectory string) {
	sessions, _ := os.ReadDir(agentDirectory)
	for _, e := range sessions {
		if _, err := strconv.ParseUint(e.Name(), 10, 32); err == nil && e.IsDir() {
			dir := filepath.Join(agentDirectory, e.Name())
			if !h.Listening(HolderPlace{Socket: filepath.Join(dir, "sock"), Directory: dir}) {
				reap(dir)
			}
		}
	}
}

// Worktree implements SessionHolders.
func (h *ProcessHolders) Worktree(ctx context.Context, repository, path string) error {
	if _, err := os.Stat(path); err == nil {
		return nil
	}
	out, err := exec.CommandContext(ctx, "git", "-C", repository, "worktree", "add", "--detach", path).CombinedOutput()
	if err != nil {
		return fmt.Errorf("%w: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

// Diagnostics implements SessionHolders.
func (h *ProcessHolders) Diagnostics(place HolderPlace) string {
	path := filepath.Join(place.Directory, holderLog)
	data, err := os.ReadFile(path)
	if err != nil {
		return "no log at " + path
	}
	if len(data) > 2000 {
		data = data[len(data)-2000:]
	}
	return strings.TrimSpace(string(data))
}

func readPid(dir string) (int, bool) {
	data, err := os.ReadFile(filepath.Join(dir, holderPidFile))
	if err != nil {
		return 0, false
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	return pid, err == nil && pid > 0
}

// retire removes what names a session's processes once they are ended.
func retire(dir string) {
	_ = os.Remove(filepath.Join(dir, holderPidFile))
	_ = os.WriteFile(filepath.Join(dir, holderGroups), nil, 0o600)
}

// reap kills each process group a gone holder recorded, so its agent does not run on unowned.
func reap(dir string) {
	data, err := os.ReadFile(filepath.Join(dir, holderGroups))
	if err != nil {
		return
	}
	for _, field := range strings.Fields(string(data)) {
		if pgid, err := strconv.Atoi(field); err == nil && pgid > 1 {
			_ = syscall.Kill(-pgid, syscall.SIGKILL)
		}
	}
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
