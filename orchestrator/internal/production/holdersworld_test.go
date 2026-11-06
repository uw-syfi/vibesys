package production

import (
	"context"
	"log/slog"
	"strings"
	"sync"

	"buf.build/go/protovalidate"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// The steps of a start where a host can crash or a holder can die.
const (
	stepSpawnBefore = "before spawn"
	stepSpawnAfter  = "after spawn"
	stepHelloBefore = "before hello"
	stepHelloAfter  = "after hello"
	stepOpenBefore  = "before open"
	stepOpenAfter   = "after open"
	stepEventBefore = "before the first event"
	stepEventAfter  = "after the first event"
)

var startSteps = []string{stepSpawnBefore, stepSpawnAfter, stepHelloBefore, stepHelloAfter, stepOpenBefore, stepOpenAfter, stepEventBefore, stepEventAfter}

// holderWorld is SessionHolders over no processes. It keeps what a real world keeps over a host's
// crash: the holders, what they were opened with, and their agents. A holder behaves as the Python
// one does, which the conformance corpus holds both to: it binds its socket unless another got there
// first, and Open starts one agent however often it is asked.
type holderWorld struct {
	mu      sync.Mutex
	holders map[string]*worldHolder // by socket
	waiting map[string]*worldHolder // spawned, and not yet bound
	// agents is every session whose agent runs.
	agents map[string]bool
	// started counts the agents ever started, by socket.
	started map[string]int

	spawns, worktrees, reaps int
	// crashes and deaths are the steps where the host panics and where the holder dies, each once.
	crashes, deaths map[string]bool
	// probes are the steps where the host looks at every place's lock, as its detection path does.
	probes map[string]bool
	// lateBinds is how many Dials find nothing although a spawned holder is on its way.
	lateBinds int
	// failSpawn, when set, is why a spawn fails.
	failSpawn error
	// protocol is what a holder spawned now speaks; hello, what it answers to Hello with when not nil.
	protocol uint32
	noHello  bool
	// outcome is what a holder emits once opened.
	outcome func(*pb.OpenSessionRequest) []*pb.SessionEvent
}

func newHolderWorld() *holderWorld {
	current := uint32(pb.SessionProtocolVersion_SESSION_PROTOCOL_VERSION_CURRENT)
	return &holderWorld{
		holders: map[string]*worldHolder{}, waiting: map[string]*worldHolder{}, agents: map[string]bool{}, started: map[string]int{},
		crashes: map[string]bool{}, deaths: map[string]bool{}, probes: map[string]bool{}, protocol: current,
		outcome: func(*pb.OpenSessionRequest) []*pb.SessionEvent {
			return []*pb.SessionEvent{{Body: &pb.SessionEvent_Opened{Opened: &pb.SessionOpened{
				ResumeToken: []byte("token"), Capabilities: &pb.Capabilities{StreamsText: true, Steers: true, Resumes: true},
			}}}}
		},
	}
}

// sidecars is a new host's launcher over this world.
func (w *holderWorld) sidecars(root string) *ProcessSidecars {
	return &ProcessSidecars{
		Root: root, Holders: w, Parts: holderParts("/skills"),
		Executable: "/bin/vibesys-host", Log: slog.New(slog.DiscardHandler),
	}
}

// holderParts are the parts of a host whose configuration names a holder and a marketplace.
func holderParts(marketplace string) Parts {
	return Parts{Find: developerBuild, Config: &pb.HostConfig{SessionHolderCommand: "/bin/holder", SkillsDirectory: marketplace}}
}

// at spends what is armed at a step: the holders die there, or the host does.
func (w *holderWorld) at(step string) {
	w.mu.Lock()
	dies, crashes, probes := w.deaths[step], w.crashes[step], w.probes[step]
	delete(w.deaths, step)
	delete(w.crashes, step)
	delete(w.probes, step)
	var places []HolderPlace
	for _, h := range w.waiting {
		places = append(places, h.place)
	}
	for _, h := range w.holders {
		places = append(places, h.place)
	}
	if dies {
		for socket, h := range w.holders {
			h.dead = true
			delete(w.holders, socket)
		}
		w.waiting = map[string]*worldHolder{}
	}
	w.mu.Unlock()
	if probes {
		// The host's detection path looks at the lock of every place, at this very point of a start.
		for _, place := range places {
			w.Alive(place)
			_ = w.Reap(place)
		}
	}
	if crashes {
		panic(daemontest.Crash{At: step})
	}
}

// worldHolder is one process that holds a session.
type worldHolder struct {
	place    HolderPlace
	protocol uint32
	noHello  bool
	// dead says the process is gone, so its lock is free. It may still be in the world's holders: a
	// socket name a dead process left answers nothing, but Dial still finds the name.
	dead bool
	// unreachable says the process lives and its calls fail for now.
	unreachable bool
	opened      *pb.OpenSessionRequest
	events      []*pb.SessionEvent
	pending     []*pb.SessionEvent
	commands    []*pb.SessionCommand
}

// bind makes what was spawned at a place listen, unless something already does there.
func (w *holderWorld) bind(socket string) {
	h, ok := w.waiting[socket]
	if !ok {
		return
	}
	delete(w.waiting, socket)
	if _, taken := w.holders[socket]; !taken {
		w.holders[socket] = h
	} else {
		h.dead = true // it could not take the lock the holder there has, and ended at once.
	}
}

// Dial implements SessionHolders.
func (w *holderWorld) Dial(_ context.Context, place HolderPlace) (HolderLink, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.lateBinds > 0 {
		w.lateBinds--
	} else {
		w.bind(place.Socket)
	}
	h, ok := w.holders[place.Socket]
	if !ok {
		return nil, ErrNoHolder
	}
	return &worldLink{w: w, h: h}, nil
}

// Spawn implements SessionHolders.
func (w *holderWorld) Spawn(place HolderPlace, _ []string, bootstrap *pb.SessionBootstrap) (<-chan struct{}, error) {
	w.at(stepSpawnBefore)
	w.mu.Lock()
	if w.failSpawn != nil {
		defer w.mu.Unlock()
		return nil, w.failSpawn
	}
	if w.aliveAt(place.Socket) {
		w.mu.Unlock()
		return nil, ErrHolderLives
	}
	w.spawns++
	if err := protovalidate.Validate(bootstrap); err != nil {
		w.mu.Unlock()
		panic("a bootstrap the contract refuses: " + err.Error())
	}
	exited := make(chan struct{})
	w.waiting[place.Socket] = &worldHolder{place: place, protocol: w.protocol, noHello: w.noHello}
	w.mu.Unlock()
	w.at(stepSpawnAfter)
	return exited, nil
}

// AwaitListening implements SessionHolders. A holder that died has closed nothing a real host would
// wait on for long: a test sees the answer at once.
func (w *holderWorld) AwaitListening(_ context.Context, place HolderPlace, _ <-chan struct{}) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.bind(place.Socket)
	if _, ok := w.holders[place.Socket]; !ok {
		return status.Error(codes.Unavailable, "it exited at start")
	}
	return nil
}

// aliveAt says a process holds the lock of the place whose socket this is: one that listens, or one
// that was spawned and has not bound yet, which takes its lock before it binds.
func (w *holderWorld) aliveAt(socket string) bool {
	if h, ok := w.holders[socket]; ok && !h.dead {
		return true
	}
	_, ok := w.waiting[socket]
	return ok
}

// Alive implements SessionHolders.
func (w *holderWorld) Alive(place HolderPlace) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.aliveAt(place.Socket)
}

// End implements SessionHolders.
func (w *holderWorld) End(place HolderPlace) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if h, ok := w.holders[place.Socket]; ok {
		h.dead = true
		delete(w.holders, place.Socket)
	}
	delete(w.waiting, place.Socket)
	delete(w.agents, place.Socket)
	return nil
}

// Reap implements SessionHolders.
func (w *holderWorld) Reap(place HolderPlace) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if !w.aliveAt(place.Socket) {
		delete(w.agents, place.Socket)
		w.reaps++
	}
	return nil
}

// ReapAgent implements SessionHolders.
func (w *holderWorld) ReapAgent(agentDirectory string) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	for socket := range w.agents {
		if !w.aliveAt(socket) && strings.HasPrefix(socket, agentDirectory+"/") {
			delete(w.agents, socket)
		}
	}
	return nil
}

// Worktree implements SessionHolders.
func (w *holderWorld) Worktree(context.Context, string, string) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.worktrees++
	return nil
}

func (w *holderWorld) startedAgents() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	total := 0
	for _, n := range w.started {
		total += n
	}
	return total
}

// liveAgentsOf says how many sessions of the agent have an agent running.
func (w *holderWorld) liveAgents() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return len(w.agents)
}

// worldLink is a host's connection to a holder of the world.
type worldLink struct {
	w *holderWorld
	h *worldHolder
}

func (l *worldLink) alive() error {
	if l.h.dead || l.h.unreachable {
		return status.Error(codes.Unavailable, "connection refused")
	}
	return nil
}

// Hello implements HolderLink.
func (l *worldLink) Hello(_ context.Context, request *pb.SessionHelloRequest) (*pb.SessionHelloResponse, error) {
	l.w.at(stepHelloBefore)
	answer, err := l.hello(request)
	if err != nil {
		return nil, err
	}
	l.w.at(stepHelloAfter)
	return answer, nil
}

func (l *worldLink) hello(request *pb.SessionHelloRequest) (*pb.SessionHelloResponse, error) {
	l.w.mu.Lock()
	defer l.w.mu.Unlock()
	if err := l.alive(); err != nil {
		return nil, err
	}
	if l.h.noHello {
		return nil, status.Error(codes.Unimplemented, "unknown method Hello")
	}
	if err := protovalidate.Validate(request); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	answer := &pb.SessionHelloResponse{
		Versions:      &pb.BuildVersions{BinaryVersion: "held", ProtocolVersion: l.h.protocol, MinProtocolVersion: l.h.protocol},
		Compatibility: holderCompatibility(request.GetClient(), l.h.protocol),
	}
	if l.h.opened != nil {
		answer.Session = l.h.opened.GetSession()
	}
	return answer, nil
}

// holderCompatibility is the rule a holder applies to who asks.
func holderCompatibility(client *pb.BuildVersions, protocol uint32) pb.Compatibility {
	switch {
	case client.GetProtocolVersion() < protocol:
		return pb.Compatibility_COMPATIBILITY_CLIENT_TOO_OLD
	case client.GetMinProtocolVersion() > protocol:
		return pb.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD
	}
	return pb.Compatibility_COMPATIBILITY_COMPATIBLE
}

// Open implements HolderLink.
func (l *worldLink) Open(_ context.Context, request *pb.OpenSessionRequest) (*pb.OpenSessionResponse, error) {
	l.w.at(stepOpenBefore)
	l.w.mu.Lock()
	if err := l.alive(); err != nil {
		l.w.mu.Unlock()
		return nil, err
	}
	if err := protovalidate.Validate(request); err != nil {
		l.w.mu.Unlock()
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	switch {
	case l.h.opened == nil:
		l.h.opened = proto.Clone(request).(*pb.OpenSessionRequest)
		l.w.started[l.h.place.Socket]++
		l.w.agents[l.h.place.Socket] = true
		l.h.pending = l.w.outcome(request)
	case !proto.Equal(l.h.opened.GetSession(), request.GetSession()):
		held := l.h.opened.GetSession()
		l.w.mu.Unlock()
		return nil, status.Errorf(codes.FailedPrecondition, "this process holds %s/%d, not %s", held.GetAgentId(), held.GetSession(), request.GetSession().GetAgentId())
	}
	answer := &pb.OpenSessionResponse{Session: l.h.opened.GetSession()}
	l.w.mu.Unlock()
	l.w.at(stepOpenAfter)
	return answer, nil
}

// Send implements HolderLink.
func (l *worldLink) Send(_ context.Context, request *pb.SendRequest) error {
	l.w.mu.Lock()
	defer l.w.mu.Unlock()
	if err := l.alive(); err != nil {
		return err
	}
	if l.h.opened == nil {
		return status.Error(codes.FailedPrecondition, "nothing was opened")
	}
	l.h.commands = append(l.h.commands, request.GetCommand())
	return nil
}

// Follow implements HolderLink.
func (l *worldLink) Follow(_ context.Context, after uint64, gone func(error) error) (daemon.EventStream, error) {
	l.w.mu.Lock()
	defer l.w.mu.Unlock()
	if err := l.alive(); err != nil {
		return nil, err
	}
	if l.h.opened == nil {
		return nil, status.Error(codes.FailedPrecondition, "nothing was opened")
	}
	return &worldStream{link: l, after: after, gone: gone}, nil
}

// Close implements HolderLink.
func (l *worldLink) Close() {}

// worldStream reads one holder's events, which are emitted as they are read for.
type worldStream struct {
	link  *worldLink
	after uint64
	gone  func(error) error
}

// Next implements daemon.EventStream.
func (s *worldStream) Next() (*pb.SessionEvent, error) {
	w, h := s.link.w, s.link.h
	w.mu.Lock()
	first := len(h.events) == 0 && len(h.pending) > 0
	w.mu.Unlock()
	if first {
		w.at(stepEventBefore)
	}
	w.mu.Lock()
	if h.dead {
		w.mu.Unlock()
		return nil, s.gone(status.Error(codes.Unavailable, "the holder died"))
	}
	for _, ev := range h.pending {
		ev = proto.Clone(ev).(*pb.SessionEvent)
		ev.Offset = uint64(len(h.events) + 1)
		h.events = append(h.events, ev)
	}
	h.pending = nil
	if s.after >= uint64(len(h.events)) {
		w.mu.Unlock()
		return nil, daemon.ErrIdle
	}
	ev := h.events[s.after]
	s.after++
	w.mu.Unlock()
	if first {
		w.at(stepEventAfter)
	}
	return ev, nil
}

// Close implements daemon.EventStream.
func (s *worldStream) Close() {}

var _ SessionHolders = (*holderWorld)(nil)

// session is what a test asks a launcher to start.
func sessionStart(agent string, role core.AgentRole, repository string) daemon.SessionStart {
	return daemon.SessionStart{
		Launch: core.LaunchSession{
			Session: core.Session{Agent: core.AgentID(agent), N: 1}, Run: "run-1", Role: role,
			Spec: core.AgentSpec{Harness: core.ClaudeCode},
		},
		Repository: repository, Task: "task",
		Tools: daemon.LaunchEnv{"VIBESYS_ORCHESTRATOR_TOKEN": "a-token"},
	}
}
