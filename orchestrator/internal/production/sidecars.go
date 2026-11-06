package production

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
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
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// EnvSidecar names how to start a sidecar: the path of an orchestrator/sidecar checkout, run with
// `uv run --frozen --project PATH python -m vs_sidecar`, or a command whose words are run as given.
// It is a developer override. Without it the sidecar of the release the host was installed in
// is used (see ReleaseSidecar).
const EnvSidecar = "VIBESYS_ORCHESTRATOR_SIDECAR"

// SidecarsFor gives the sidecar launcher the environment and the release layout name, or the
// launcher that refuses every launch with the reason when a setting is missing or wrong. Sessions
// keep their files under root. executable is the host binary, where the release layout is looked
// for.
func SidecarsFor(getenv func(string) string, root, executable string, files Files, log *slog.Logger) (daemon.Sidecars, error) {
	argv, err := sidecarCommand(getenv, executable, files)
	if err == nil {
		var plugin Plugin
		if plugin, err = PluginFor(getenv, executable, files); err == nil {
			if err = os.MkdirAll(root, 0o700); err != nil {
				return nil, err
			}
			return &ProcessSidecars{Root: root, Command: argv, Plugin: plugin, Executable: executable, Log: log}, nil
		}
	}
	log.Warn("no sidecar launcher", "reason", err)
	return refusedSidecars{reason: err.Error()}, nil
}

// sidecarCommand is EnvSidecar when it is set, whatever it holds, and otherwise the release's
// own sidecar. A set but wrong value is never replaced by the release.
func sidecarCommand(getenv func(string) string, executable string, files Files) ([]string, error) {
	if setting := strings.TrimSpace(getenv(EnvSidecar)); setting != "" {
		return sidecarSetting(setting)
	}
	python, err := ReleaseSidecar(executable, files)
	if err != nil {
		return nil, fmt.Errorf("%s is not set and %w, so no agent session can start: set %s to an orchestrator/sidecar checkout or to the command that runs the sidecar", EnvSidecar, err, EnvSidecar)
	}
	return []string{python, "-m", "vs_sidecar"}, nil
}

func sidecarSetting(setting string) ([]string, error) {
	if info, err := os.Stat(setting); err == nil && info.IsDir() {
		if _, err := os.Stat(filepath.Join(setting, "pyproject.toml")); err != nil {
			return nil, fmt.Errorf("%s=%s is a directory without pyproject.toml, so it is not an orchestrator/sidecar checkout", EnvSidecar, setting)
		}
		return []string{"uv", "run", "--frozen", "--quiet", "--project", setting, "python", "-m", "vs_sidecar"}, nil
	}
	return strings.Fields(setting), nil
}

type refusedSidecars struct{ reason string }

func (r refusedSidecars) Launch(context.Context, core.LaunchSession, daemon.LaunchEnv) (daemon.Sidecar, error) {
	return nil, errors.New(r.reason)
}

func (refusedSidecars) Attach(context.Context, core.Session) (daemon.Sidecar, error) {
	return nil, daemon.ErrSidecarGone
}

// ProcessSidecars starts one detached sidecar process per session and reaches it over the Unix
// socket in the session's directory, ROOT/AGENT/N, where AGENT is a digest of the agent id that
// keeps the socket path short. A sidecar outlives the daemon; a daemon that restarts finds it by
// that directory. When a sidecar is gone, the agent processes it left are killed through the
// process groups it recorded in agent.pgids, before any new session of that agent starts.
type ProcessSidecars struct {
	Root    string
	Command []string
	// Plugin is what every session loads. It has no default: a launcher without one is refused
	// at SidecarsFor.
	Plugin     Plugin
	Executable string
	Log        *slog.Logger

	mu    sync.Mutex
	conns map[core.Session]*sidecarProcess
}

// MCPServer is one stdio tool server in the sidecar's --mcp-config file.
type MCPServer struct {
	Name    string            `json:"name"`
	Command string            `json:"command"`
	Args    []string          `json:"args"`
	Env     map[string]string `json:"env"`
}

func (s *ProcessSidecars) agentDir(agent core.AgentID) string {
	sum := sha256.Sum256([]byte(agent))
	return filepath.Join(s.Root, hex.EncodeToString(sum[:8]))
}

func (s *ProcessSidecars) sessionDir(session core.Session) string {
	return filepath.Join(s.agentDir(session.Agent), strconv.FormatUint(uint64(session.N), 10))
}

// Launch implements daemon.Sidecars.
func (s *ProcessSidecars) Launch(ctx context.Context, l core.LaunchSession, env daemon.LaunchEnv) (daemon.Sidecar, error) {
	if sc, err := s.Attach(ctx, l.Session); err == nil {
		return sc, nil
	}
	dir := s.sessionDir(l.Session)
	if sock := filepath.Join(dir, "sock"); len(sock) > maxSocketPath {
		return nil, fmt.Errorf("the sidecar socket path %s is longer than %d bytes, which a Unix socket cannot hold: set a shorter VIBESYS_ORCHESTRATOR_STATE_DIR", sock, maxSocketPath)
	}
	s.reapAgent(l.Session.Agent)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	launch, err := s.launchSpec(ctx, l)
	if err != nil {
		return nil, err
	}
	data, err := protojson.Marshal(launch)
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(filepath.Join(dir, "launch.json"), data, 0o600); err != nil {
		return nil, err
	}
	tools, err := json.Marshal([]MCPServer{{Name: "vibesys", Command: s.Executable, Args: []string{"mcp"}, Env: env}})
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(filepath.Join(dir, "mcp.json"), tools, 0o600); err != nil {
		return nil, err
	}
	for _, name := range []string{"sidecar.pid", "sock"} {
		_ = os.Remove(filepath.Join(dir, name))
	}
	var out bytes.Buffer
	cmd := s.command(ctx, dir)
	cmd.Stdout, cmd.Stderr = &out, &out
	// The sidecar detaches itself (it forks twice), so this returns once it is on its own.
	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("start the sidecar with %s: %w: %s", strings.Join(s.Command, " "), err, strings.TrimSpace(out.String()))
	}
	return s.awaitReady(ctx, l.Session, dir)
}

// launchSpec is the launch file for a session: the effect's launch, the agent's worktree (made on
// its first session), and the plugin the session loads. The plugin home is kept with the agent, not
// the session, so a resume finds the one the first session installed into, which also holds the
// provider's conversation.
func (s *ProcessSidecars) launchSpec(ctx context.Context, l core.LaunchSession) (*pb.LaunchSession, error) {
	launch, err := launchMessage(l)
	if err != nil {
		return nil, err
	}
	if launch.Worktree == "" {
		if launch.Worktree, err = s.worktree(ctx, l); err != nil {
			return nil, err
		}
	}
	home := filepath.Join(s.agentDir(l.Session.Agent), "plugin-home")
	if !filepath.IsAbs(home) {
		return nil, fmt.Errorf("launch.plugin.home %s is not absolute: set VIBESYS_ORCHESTRATOR_STATE_DIR to an absolute path", home)
	}
	launch.Plugin = &pb.SessionPlugin{Marketplace: s.Plugin.Marketplace, Name: s.Plugin.Name, Home: home}
	return launch, nil
}

// command is the process that starts a session's sidecar in dir. It inherits the daemon's
// environment and nothing of the launch environment: what the agent's shell sees is built by the
// sidecar from the launch message, not from this process.
func (s *ProcessSidecars) command(ctx context.Context, dir string) *exec.Cmd {
	argv := append(append([]string(nil), s.Command...),
		"--launch", filepath.Join(dir, "launch.json"), "--state-dir", dir, "--socket", filepath.Join(dir, "sock"),
		"--mcp-config", filepath.Join(dir, "mcp.json"), "--detach")
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	return cmd
}

// awaitReady waits, within ctx, for the sidecar to write its pid and answer on its socket.
func (s *ProcessSidecars) awaitReady(ctx context.Context, session core.Session, dir string) (daemon.Sidecar, error) {
	for {
		if pid, ok := readPid(dir); ok {
			if !running(pid, dir) {
				return nil, fmt.Errorf("the sidecar exited at start: %s", tail(filepath.Join(dir, "sidecar.log")))
			}
			if conn, err := (&net.Dialer{}).DialContext(ctx, "unix", filepath.Join(dir, "sock")); err == nil {
				conn.Close()
				return s.connect(session, dir, pid)
			}
		}
		select {
		case <-ctx.Done():
			return nil, fmt.Errorf("the sidecar did not answer on %s: %w: %s", filepath.Join(dir, "sock"), ctx.Err(), tail(filepath.Join(dir, "sidecar.log")))
		case <-time.After(50 * time.Millisecond):
		}
	}
}

// Attach implements daemon.Sidecars.
func (s *ProcessSidecars) Attach(_ context.Context, session core.Session) (daemon.Sidecar, error) {
	dir := s.sessionDir(session)
	pid, ok := readPid(dir)
	if !ok || !running(pid, dir) {
		reap(dir)
		return nil, daemon.ErrSidecarGone
	}
	return s.connect(session, dir, pid)
}

func (s *ProcessSidecars) connect(session core.Session, dir string, pid int) (daemon.Sidecar, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if c, ok := s.conns[session]; ok && c.pid == pid {
		return c, nil
	}
	conn, err := grpc.NewClient("unix://"+filepath.Join(dir, "sock"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	if s.conns == nil {
		s.conns = map[core.Session]*sidecarProcess{}
	}
	if old, ok := s.conns[session]; ok {
		old.conn.Close()
	}
	c := &sidecarProcess{dir: dir, pid: pid, conn: conn, client: pb.NewSidecarServiceClient(conn), log: s.Log.With("sidecar", dir)}
	s.conns[session] = c
	return c, nil
}

// reapAgent kills what every earlier, gone sidecar of an agent left running, so no old agent
// process works in a worktree under a new session.
func (s *ProcessSidecars) reapAgent(agent core.AgentID) {
	sessions, _ := os.ReadDir(s.agentDir(agent))
	for _, e := range sessions {
		if _, err := strconv.ParseUint(e.Name(), 10, 32); err == nil && e.IsDir() {
			dir := filepath.Join(s.agentDir(agent), e.Name())
			if pid, ok := readPid(dir); !ok || !running(pid, dir) {
				reap(dir)
			}
		}
	}
}

// worktree gives a first session its directory: the repository itself for the lead, and a
// detached git worktree of it, kept with the agent's sessions, for every other agent.
func (s *ProcessSidecars) worktree(ctx context.Context, l core.LaunchSession) (string, error) {
	if l.Role == core.RoleLead {
		return l.Repository, nil
	}
	path := filepath.Join(s.agentDir(l.Session.Agent), "worktree")
	if _, err := os.Stat(path); err == nil {
		return path, nil
	}
	out, err := exec.CommandContext(ctx, "git", "-C", l.Repository, "worktree", "add", "--detach", path).CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("create a worktree of %s for agent %s: %w: %s", l.Repository, l.Session.Agent, err, strings.TrimSpace(string(out)))
	}
	return path, nil
}

func launchMessage(l core.LaunchSession) (*pb.LaunchSession, error) {
	data, err := wire.EncodeEffect(l)
	if err != nil {
		return nil, err
	}
	var e pb.Effect
	if err := proto.Unmarshal(data, &e); err != nil {
		return nil, err
	}
	return e.GetLaunchSession(), nil
}

// sidecarProcess is the daemon's side of one running sidecar.
type sidecarProcess struct {
	dir    string
	pid    int
	conn   *grpc.ClientConn
	client pb.SidecarServiceClient
	log    *slog.Logger
}

func (c *sidecarProcess) gone(err error) error {
	if !running(c.pid, c.dir) {
		return daemon.ErrSidecarGone
	}
	return err
}

// Send implements daemon.Sidecar.
func (c *sidecarProcess) Send(ctx context.Context, cmd *pb.SidecarCommand) error {
	if _, err := c.client.Send(ctx, &pb.SendRequest{Command: cmd}); err != nil {
		return c.gone(err)
	}
	return nil
}

// Follow implements daemon.Sidecar. A goroutine reads the stream into a buffer, so Next never
// waits; the buffer is bounded, and a full one holds the stream back through flow control.
func (c *sidecarProcess) Follow(_ context.Context, after uint64) (daemon.EventStream, error) {
	ctx, cancel := context.WithCancel(context.Background())
	stream, err := c.client.Follow(ctx, &pb.FollowRequest{AfterOffset: after})
	if err != nil {
		cancel()
		return nil, c.gone(err)
	}
	f := &followStream{events: make(chan *pb.SidecarEvent, 256), cancel: cancel, owner: c}
	c.log.Info("following the sidecar", "after", after)
	go func() {
		for {
			resp, err := stream.Recv()
			if err != nil {
				c.log.Info("the sidecar stream ended", "err", err)
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

// Kill implements daemon.Sidecar. It ends the sidecar and the agent processes it recorded, and
// leaves nothing that names either, so a repeat, or a later look at the directory, finds nothing to
// kill: a process id the system has since given to another program is never signalled.
func (c *sidecarProcess) Kill(context.Context) error {
	if running(c.pid, c.dir) {
		if err := syscall.Kill(c.pid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
			return err
		}
	}
	reap(c.dir)
	retire(c.dir)
	c.log.Info("the sidecar was ended")
	return nil
}

type followStream struct {
	events chan *pb.SidecarEvent
	end    errorBox
	cancel context.CancelFunc
	owner  *sidecarProcess
}

func (f *followStream) Next() (*pb.SidecarEvent, error) {
	select {
	case ev, ok := <-f.events:
		if ok {
			return ev, nil
		}
		err := f.end.Load()
		if errors.Is(err, io.EOF) {
			err = errors.New("the sidecar ended its stream")
		}
		return nil, f.owner.gone(err)
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

func readPid(dir string) (int, bool) {
	data, err := os.ReadFile(filepath.Join(dir, "sidecar.pid"))
	if err != nil {
		return 0, false
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	return pid, err == nil && pid > 0
}

func alive(pid int) bool {
	err := syscall.Kill(pid, 0)
	return err == nil || errors.Is(err, syscall.EPERM)
}

// running says pid is the sidecar of dir and not a program that was given the number after the
// sidecar exited. Where the system names a process's command line, it must carry the directory,
// which every sidecar is started with (`--state-dir DIR`, whatever program the setting names);
// elsewhere liveness is all there is to go on.
func running(pid int, dir string) bool {
	if !alive(pid) {
		return false
	}
	if _, err := os.Stat("/proc/self"); err != nil {
		return true
	}
	cmdline, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/cmdline")
	return err == nil && commandOwns(cmdline, dir)
}

// commandOwns says a NUL-separated command line has dir as a whole argument.
func commandOwns(cmdline []byte, dir string) bool {
	return bytes.Contains(cmdline, []byte("\x00"+dir+"\x00"))
}

// retire removes what names a session's processes once they are ended.
func retire(dir string) {
	_ = os.Remove(filepath.Join(dir, "sidecar.pid"))
	_ = os.WriteFile(filepath.Join(dir, "agent.pgids"), nil, 0o600)
}

// reap kills each process group a gone sidecar recorded, so its agent does not run on unowned.
func reap(dir string) {
	data, err := os.ReadFile(filepath.Join(dir, "agent.pgids"))
	if err != nil {
		return
	}
	for _, field := range strings.Fields(string(data)) {
		if pgid, err := strconv.Atoi(field); err == nil && pgid > 1 {
			_ = syscall.Kill(-pgid, syscall.SIGKILL)
		}
	}
}

func tail(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return "no log at " + path
	}
	if len(data) > 2000 {
		data = data[len(data)-2000:]
	}
	return strings.TrimSpace(string(data))
}
