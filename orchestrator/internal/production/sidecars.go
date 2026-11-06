package production

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"buf.build/go/protovalidate"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// SettingSessionHolder is the field of the host's configuration that says how to start a sidecar: the
// path of an orchestrator/sidecar checkout, run with `uv run --frozen --project PATH python -m
// vs_sidecar`, or a command whose words are run as given. It is a developer override. Without it the
// sidecar of the release the host was installed in is used (see release.Release.SessionHolderCommand).
const SettingSessionHolder = "session_holder_command"

// SidecarsFor gives the sidecar launcher. Sessions keep their files under root. executable is the
// host binary, which each agent's tool server runs. The launcher resolves its parts on every
// launch, so a part that is missing refuses that launch with the reason, and the next launch, after
// the install is repaired, goes through.
func SidecarsFor(parts Parts, root, executable string, log *slog.Logger) (*ProcessSidecars, error) {
	if err := os.MkdirAll(root, 0o700); err != nil {
		return nil, err
	}
	return &ProcessSidecars{Root: root, Parts: parts, Holders: &ProcessHolders{Log: log}, Executable: executable, Log: log}, nil
}

// sidecarCommand is the setting when there is one, whatever it holds, and otherwise the release's
// own sidecar. A set but wrong value is never replaced by the release.
func sidecarCommand(setting string, find release.Finder) ([]string, error) {
	if setting = strings.TrimSpace(setting); setting != "" {
		return sidecarSetting(setting)
	}
	rel, err := find()
	if err == nil {
		var command []string
		if command, err = rel.SessionHolderCommand(); err == nil {
			return command, nil
		}
	}
	return nil, fmt.Errorf("the host's configuration names no %s and %w, so no agent session can start: choose an orchestrator/sidecar checkout or the command that runs the sidecar with `vibesys-client host configure --session-holder`", SettingSessionHolder, err)
}

func sidecarSetting(setting string) ([]string, error) {
	if info, err := os.Stat(setting); err == nil && info.IsDir() {
		if _, err := os.Stat(filepath.Join(setting, "pyproject.toml")); err != nil {
			return nil, fmt.Errorf("%s=%s is a directory without pyproject.toml, so it is not an orchestrator/sidecar checkout", SettingSessionHolder, setting)
		}
		return []string{"uv", "run", "--frozen", "--quiet", "--project", setting, "python", "-m", "vs_sidecar"}, nil
	}
	return strings.Fields(setting), nil
}

// ProcessSidecars starts one holder per session and reaches it over the socket in the session's
// directory, ROOT/AGENT/N, where AGENT is a digest of the agent id that keeps the socket path short.
// A holder outlives the host; a host that restarts finds it by that place and asks it who it is. It
// is the shell's whole knowledge of the start: find or start the holder, Hello, Open. The daemon sees
// none of it, only a session that started and then opened or failed with a reason.
type ProcessSidecars struct {
	Root string
	// Parts gives the command that holds a session and the plugin every session loads. A launch
	// with a part that cannot be resolved is refused.
	Parts Parts
	// Holders is the world the holders live in.
	Holders    SessionHolders
	Executable string
	Log        *slog.Logger

	mu       sync.Mutex
	conns    map[core.Session]*heldSession
	starting map[core.Session]<-chan struct{}
}

func (s *ProcessSidecars) agentDir(agent core.AgentID) string {
	sum := sha256.Sum256([]byte(agent))
	return filepath.Join(s.Root, hex.EncodeToString(sum[:8]))
}

func (s *ProcessSidecars) place(session core.Session) HolderPlace {
	dir := filepath.Join(s.agentDir(session.Agent), strconv.FormatUint(uint64(session.N), 10))
	return HolderPlace{Socket: filepath.Join(dir, "sock"), Directory: dir}
}

// Launch implements daemon.Sidecars. Its parts and what it asks for are checked first, so a missing
// part or a bad field is refused by name before anything starts. Open is repeatable, so a launch delivered again, by this host or by
// one that came after a crash, asks again and starts nothing new.
func (s *ProcessSidecars) Launch(ctx context.Context, start daemon.SessionStart) (daemon.Sidecar, error) {
	command, _, err := s.Parts.SessionHolder()
	if err != nil {
		return nil, err
	}
	plugin, _, err := s.Parts.Skills()
	if err != nil {
		return nil, err
	}
	session := start.Launch.Session
	place := s.place(session)
	if len(place.Socket) > maxSocketPath {
		return nil, &daemon.LaunchError{Reason: core.StartStateLocationTooLong, Err: fmt.Errorf("the agent session's socket path %s is longer than %d bytes, which a Unix socket cannot hold", place.Socket, maxSocketPath)}
	}
	request, err := s.openRequest(start, plugin)
	if err != nil {
		return nil, err
	}
	if err := protovalidate.Validate(request); err != nil {
		return nil, fmt.Errorf("the session's start is invalid: %w", err)
	}
	if start.Launch.Role != core.RoleLead {
		if err := s.Holders.Worktree(ctx, start.Repository, request.GetWorkingDirectory()); err != nil {
			return nil, &daemon.LaunchError{Reason: core.StartWorkingDirUnavailable, Err: fmt.Errorf("create a worktree of %s for agent %s: %w", start.Repository, session.Agent, err)}
		}
	}
	link, err := s.holderFor(ctx, session, place, command)
	if err != nil {
		return nil, err
	}
	if _, err := link.Open(ctx, request); err != nil {
		link.Close()
		return nil, s.refusedOpen(err)
	}
	return s.held(session, place, link), nil
}

// refusedOpen reads the answer to an Open that was not taken. A call that did not reach an answer
// is the caller's to retry; a refusal already names the field.
func (s *ProcessSidecars) refusedOpen(err error) error {
	switch status.Code(err) {
	case codes.InvalidArgument, codes.FailedPrecondition:
		return &daemon.LaunchError{Reason: core.StartOther, Err: fmt.Errorf("the session refused its start: %s", status.Convert(err).Message())}
	default:
		return fmt.Errorf("open the agent session: %w", err)
	}
}

// holderFor is the holder of a session: the one that answers at its place, or one it starts there.
// A process it started that has not begun to listen yet is waited for and not started again.
func (s *ProcessSidecars) holderFor(ctx context.Context, session core.Session, place HolderPlace, command []string) (HolderLink, error) {
	link, hello, err := s.ask(ctx, place)
	if errors.Is(err, ErrNoHolder) {
		// A process started by an earlier host that crashed before the session opened holds the
		// place's lock and may not listen yet: it is waited for, and no second one is started.
		exited := s.startingProcess(session)
		if exited == nil && !s.Holders.Alive(place) {
			if err := s.Holders.ReapAgent(s.agentDir(session.Agent)); err != nil {
				return nil, fmt.Errorf("end the agent processes an earlier session left: %w", err)
			}
			s.forgetConn(session)
			if exited, err = s.Holders.Spawn(place, command, place.Bootstrap()); err != nil {
				return nil, fmt.Errorf("start the agent session: %w", err)
			}
			s.mu.Lock()
			if s.starting == nil {
				s.starting = map[core.Session]<-chan struct{}{}
			}
			s.starting[session] = exited
			s.mu.Unlock()
		}
		if err := s.Holders.AwaitListening(ctx, place, exited); err != nil {
			return nil, fmt.Errorf("the agent session did not answer on %s: %w (its output is in %s)", place.Socket, err, place.LogFile())
		}
		link, hello, err = s.ask(ctx, place)
	}
	if err != nil {
		return nil, s.unusable(place, err)
	}
	if hello.GetSession() != nil && !sameSession(hello.GetSession(), session) {
		link.Close()
		return nil, &daemon.LaunchError{Reason: core.StartOther, Err: fmt.Errorf("session %s/%d answers on the socket of %s/%d", hello.GetSession().GetAgentId(), hello.GetSession().GetSession(), session.Agent, session.N)}
	}
	return link, nil
}

// unusable ends a holder that cannot work with this host, whatever it was doing: nothing can drive
// it, and an agent left running would hold the worktree against the session that replaces it. Any
// other error passes through.
func (s *ProcessSidecars) unusable(place HolderPlace, err error) error {
	var incompatible *incompatibleError
	if !errors.As(err, &incompatible) {
		return err
	}
	if end := s.Holders.End(place); end != nil {
		return fmt.Errorf("end the incompatible agent session: %w", end)
	}
	return &daemon.LaunchError{Reason: core.StartIncompatibleVersions, Err: err}
}

// incompatibleError says what answers at a place and this host share no protocol.
type incompatibleError struct{ detail string }

func (e *incompatibleError) Error() string { return e.detail }

// helloBound is how long a holder has to answer who it is. Hello does no work and a holder serves it
// on a thread of its own, so a holder that takes longer is hung, and a caller's longer bound must not
// be spent on it.
const helloBound = 10 * time.Second

// ask says who answers at a place: a link and its Hello, ErrNoHolder when nothing does, an
// *incompatibleError when it cannot work with this host, and any other error when it is not known.
func (s *ProcessSidecars) ask(ctx context.Context, place HolderPlace) (HolderLink, *pb.SessionHelloResponse, error) {
	link, err := s.Holders.Dial(ctx, place)
	if err != nil {
		return nil, nil, err
	}
	hctx, cancel := context.WithTimeout(ctx, helloBound)
	defer cancel()
	hello, err := link.Hello(hctx, &pb.SessionHelloRequest{Client: sessionVersions()})
	if err != nil {
		link.Close()
		switch status.Code(err) {
		case codes.Unavailable:
			return nil, nil, ErrNoHolder // it was going as it was reached.
		case codes.Unimplemented:
			return nil, nil, &incompatibleError{detail: "the agent session at " + place.Socket + " is of a release with no version handshake, and this host cannot work with it"}
		default:
			return nil, nil, fmt.Errorf("ask who holds %s: %w", place.Socket, err)
		}
	}
	if c := hello.GetCompatibility(); c != pb.Compatibility_COMPATIBILITY_COMPATIBLE {
		link.Close()
		return nil, nil, &incompatibleError{detail: fmt.Sprintf(
			"the agent session at %s and this host cannot work together (%s): it is version %s, protocol %d; this host is version %s, protocol %d",
			place.Socket, c, hello.GetVersions().GetBinaryVersion(), hello.GetVersions().GetProtocolVersion(), Version, sessionVersions().GetProtocolVersion(),
		)}
	}
	return link, hello, nil
}

// sessionVersions is what this host says it speaks to a holder.
func sessionVersions() *pb.BuildVersions {
	current := uint32(pb.SessionProtocolVersion_SESSION_PROTOCOL_VERSION_CURRENT)
	return &pb.BuildVersions{BinaryVersion: Version, ProtocolVersion: current, MinProtocolVersion: current}
}

func sameSession(ref *pb.SessionRef, session core.Session) bool {
	return ref.GetAgentId() == string(session.Agent) && ref.GetSession() == session.N
}

// Attach implements daemon.Sidecars. A holder that never opened has no session to find: the launch
// that started it was settled or is no longer wanted, so it is ended.
func (s *ProcessSidecars) Attach(ctx context.Context, session core.Session) (daemon.Sidecar, error) {
	place := s.place(session)
	link, hello, err := s.ask(ctx, place)
	switch {
	case errors.Is(err, ErrNoHolder):
		if s.Holders.Alive(place) {
			// Its process lives and does not answer yet. It is not written off, or an agent would be left
			// running beside the session that replaces it.
			return nil, fmt.Errorf("the agent session's process lives and does not answer on %s", place.Socket)
		}
		if err := s.Holders.Reap(place); err != nil {
			return nil, err
		}
		s.forgetConn(session)
		return nil, daemon.ErrSidecarGone
	case err != nil:
		return nil, s.unusable(place, err)
	}
	if !sameSession(hello.GetSession(), session) {
		link.Close()
		if err := s.Holders.End(place); err != nil {
			return nil, err
		}
		return nil, daemon.ErrSidecarGone
	}
	return s.held(session, place, link), nil
}

// held keeps one connection per session, so the daemon's handle stays the same across asks.
func (s *ProcessSidecars) held(session core.Session, place HolderPlace, link HolderLink) *heldSession {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.starting, session)
	if c, ok := s.conns[session]; ok {
		link.Close()
		return c
	}
	if s.conns == nil {
		s.conns = map[core.Session]*heldSession{}
	}
	c := &heldSession{owner: s, session: session, place: place, link: link, log: s.Log.With("session", place.Directory)}
	s.conns[session] = c
	return c
}

func (s *ProcessSidecars) startingProcess(session core.Session) <-chan struct{} {
	s.mu.Lock()
	defer s.mu.Unlock()
	exited, ok := s.starting[session]
	if !ok {
		return nil
	}
	select {
	case <-exited:
		delete(s.starting, session)
		return nil
	default:
		return exited
	}
}

func (s *ProcessSidecars) forgetConn(session core.Session) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if c, ok := s.conns[session]; ok {
		c.link.Close()
		delete(s.conns, session)
	}
}

// secretEnv names the entries of a session's tool environment that are credentials. The token is the
// only one: the rest says where the daemon is and who is asking. The name is the one the credentials
// message encodes its token under, so there is one definition of it.
func secretEnv(tools *pb.ToolServerCredentials) []string {
	if tools.GetToken() == "" {
		return nil
	}
	return []string{envcodec.NameOf(tools, "token")}
}

// openRequest is what a session is opened with. The plugin's home is kept with the agent, not the
// session, so a resume finds the one the first session installed into, which also holds what the
// agent needs to continue. It does no I/O.
func (s *ProcessSidecars) openRequest(start daemon.SessionStart, plugin Plugin) (*pb.OpenSessionRequest, error) {
	l := start.Launch
	launch, err := launchMessage(l)
	if err != nil {
		return nil, err
	}
	workdir := start.Repository
	if l.Role != core.RoleLead {
		workdir = filepath.Join(s.agentDir(l.Session.Agent), "worktree")
	}
	environment, err := agentEnvironment(start)
	if err != nil {
		return nil, err
	}
	toolEnv, err := toolServerEnvironment(start.Tools, environment)
	if err != nil {
		return nil, err
	}
	return &pb.OpenSessionRequest{
		Session: launch.GetSession(), Harness: launch.GetHarness(), Model: launch.GetModel(), ReasoningEffort: launch.GetReasoningEffort(),
		WorkingDirectory: workdir, AgentDataDirectory: filepath.Join(s.agentDir(l.Session.Agent), "plugin-home"), ResumeToken: launch.GetResumeToken(),
		Skills: &pb.SessionSkills{Directory: plugin.Marketplace, Name: plugin.Name},
		Tools: []*pb.ToolServer{{
			Name: ToolServerName, Argv: []string{s.Executable, "mcp"}, Env: toolEnv, SecretEnv: secretEnv(start.Tools),
		}},
		Environment: environment,
	}, nil
}

// ToolServerName is the name every agent's tool server is registered under, so the tools show up
// as mcp__vibesys__spawn_agent and so on. The harness skills name it; a test holds the two together.
const ToolServerName = "vibesys"

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

// heldSession is the daemon's side of one running session.
type heldSession struct {
	owner   *ProcessSidecars
	session core.Session
	place   HolderPlace
	link    HolderLink
	log     *slog.Logger
}

// gone reads an error from a holder that did not answer. A free lock says the holder's process is
// gone, at once and whatever the error was: waiting for a stream to end or a retry to give up is not
// how the host finds out. Its agents are ended on the way, so none runs on unowned.
func (c *heldSession) gone(err error) error {
	if c.owner.Holders.Alive(c.place) {
		return err
	}
	reason := "its process is no longer running"
	if reapErr := c.owner.Holders.Reap(c.place); reapErr != nil {
		reason += "; " + reapErr.Error()
	}
	return &daemon.GoneError{Reason: reason}
}

// Send implements daemon.Sidecar.
func (c *heldSession) Send(ctx context.Context, cmd *pb.SessionCommand) error {
	if err := c.link.Send(ctx, &pb.SendRequest{Command: cmd}); err != nil {
		return c.gone(err)
	}
	return nil
}

// Follow implements daemon.Sidecar.
func (c *heldSession) Follow(ctx context.Context, after uint64) (daemon.EventStream, error) {
	stream, err := c.link.Follow(ctx, after, func(err error) error { return c.gone(err) })
	if err != nil {
		return nil, c.gone(err)
	}
	c.log.Info("following the session", "after", after)
	return stream, nil
}

// Kill implements daemon.Sidecar. It ends the holder and the agent processes it recorded, and leaves
// nothing that names either, so a repeat, or a later look at the directory, finds nothing to kill.
func (c *heldSession) Kill(context.Context) error {
	if err := c.owner.Holders.End(c.place); err != nil {
		return err
	}
	c.owner.forgetConn(c.session)
	c.log.Info("the session was ended")
	return nil
}
