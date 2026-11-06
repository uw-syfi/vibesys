package production

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"path"
	"slices"
	"strings"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon/clidaemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// worldTool is one vibesys-cli a World can run: a build, and whether what runs it makes its daemon a sandbox
// daemon, as a wrapper script in front of the binary does.
type worldTool struct {
	Build   string
	Sandbox bool
}

// worldDaemon is a daemon that runs in a World.
type worldDaemon struct {
	Dir, Build, StateHome string
	Sandbox               bool
	// Jobs is how many jobs it runs. A daemon of another build with any is refused, not replaced.
	Jobs int
	// Generation counts the daemons started in the World; a replaced daemon is a new one.
	Generation int
	service    *clidaemontest.Fake
}

// jobToolWorld is the job tool and its daemons as they look from outside: a Runner for both a host's `daemon
// ensure --json` and an agent's commands, and a Connector for the sockets it answers. It does what the
// real tool does and says so the way the real tool does, from the environment each process is run
// with, so a test sees which daemon a given environment reaches. cli/cmd/vibesys-cli's real tier holds
// the tool itself to the same behavior (TestRealAnAgentReachesTheDaemonItWasTold).
type jobToolWorld struct {
	Tools map[string]worldTool
	// UID is the user, which the default runtime directory is named for.
	UID int

	daemons     map[string]*worldDaemon
	places      map[string]string
	stores      map[string][]*pb.TaskFact
	unreachable map[string]bool
	holder      string
	generations int
	// Ensured is the runtime directory of each `daemon ensure` the World answered or refused.
	Ensured []string
}

// newWorld is a world in which only the tools run, and no daemon does yet.
func newWorld(tools map[string]worldTool) *jobToolWorld {
	return &jobToolWorld{
		Tools: tools, UID: 7, daemons: map[string]*worldDaemon{}, places: map[string]string{},
		stores: map[string][]*pb.TaskFact{}, unreachable: map[string]bool{},
	}
}

// ensureCommand is the one command that may record a directory's places for a process told to use it.
var ensureCommand = []string{"daemon", "ensure", "--json"}

// exitNoAnswer is the status of a command that got no answer.
var exitNoAnswer = errors.New("exit status 4")

// Run implements what production calls a Runner: it runs the tool at program with env. Any command but
// `daemon ensure --json` is a command an agent or a person runs that needs a daemon, and adds a fact to
// the store of the daemon it reached.
func (w *jobToolWorld) Run(_ context.Context, program string, args, env []string) (stdout, stderr []byte, err error) {
	tool, ok := w.Tools[program]
	if !ok {
		return nil, nil, &fs.PathError{Op: "fork/exec", Path: program, Err: fs.ErrNotExist}
	}
	getenv := envcodec.FromEnviron(env)
	place := &pb.JobToolPlace{}
	if err := envcodec.Decode(getenv, place); err != nil {
		return refuse(pb.FailureKind_FAILURE_KIND_INVALID_INPUT, err.Error())
	}
	dir, told := place.GetRuntimeDirectory(), place.GetRuntimeDirectory() != ""
	if !told {
		dir = w.defaultDirectory(getenv)
	}
	ensure := slices.Equal(args, ensureCommand)
	if ensure {
		w.Ensured = append(w.Ensured, dir)
		if _, recorded := w.places[dir]; !recorded {
			if err := w.record(dir, getenv); err != nil {
				return refuse(pb.FailureKind_FAILURE_KIND_INVALID_INPUT, err.Error())
			}
		}
	}
	if w.unreachable[dir] {
		return refuse(pb.FailureKind_FAILURE_KIND_UNAVAILABLE, "a daemon holds this machine and does not answer; see `daemon status` and the daemon log")
	}
	d := w.daemons[dir]
	switch {
	case d == nil:
	case d.Build == tool.Build:
	case d.Jobs > 0:
		return refuse(pb.FailureKind_FAILURE_KIND_DAEMON_OTHER_BUILD, fmt.Sprintf(
			"the daemon that runs is of build %s and has %d jobs, and this command is of build %s; wait for them, or `daemon restart`", d.Build, d.Jobs, tool.Build))
	default:
		w.Kill(dir)
		d = nil
	}
	if d == nil {
		if d, err = w.start(dir, tool.Build, tool.Sandbox, told, getenv); err != nil {
			return refuse(pb.FailureKind_FAILURE_KIND_UNAVAILABLE, "the daemon did not start: "+err.Error())
		}
	}
	if ensure {
		out, err := protojson.Marshal(&pb.DaemonEnsureResponse{
			Socket: path.Join(dir, "daemon.sock"), RuntimeDirectory: dir,
			Versions: &pb.BuildVersions{BinaryVersion: d.Build, ProtocolVersion: 3, MinProtocolVersion: 1},
		})
		return out, nil, err
	}
	w.stores[d.StateHome] = append(w.stores[d.StateHome], jobFact(len(w.stores[d.StateHome])))
	d.service.SetFacts(w.stores[d.StateHome])
	return nil, nil, nil
}

// refuse is what the tool does with no answer: a Failure on stderr and exit 4.
func refuse(kind pb.FailureKind, message string) ([]byte, []byte, error) {
	failure, err := protojson.Marshal(&pb.Failure{Kind: kind, Message: message})
	if err != nil {
		return nil, nil, err
	}
	return nil, failure, exitNoAnswer
}

func jobFact(n int) *pb.TaskFact {
	return &pb.TaskFact{Fact: &pb.TaskFact_Job{Job: &pb.Job{Id: fmt.Sprintf("job-%d", n), State: pb.JobState_JOB_STATE_RUNNING}}}
}

// defaultDirectory is the runtime directory of a process told none.
func (w *jobToolWorld) defaultDirectory(getenv envcodec.Lookup) string {
	if rt := getenv("XDG_RUNTIME_DIR"); path.IsAbs(rt) {
		return path.Join(rt, "vibesys-cli")
	}
	return fmt.Sprintf("/tmp/vibesys-cli-%d", w.UID)
}

// record writes a directory's places from an environment.
func (w *jobToolWorld) record(dir string, getenv envcodec.Lookup) error {
	home := path.Join(getenv("HOME"), ".local", "state")
	if state := getenv("XDG_STATE_HOME"); path.IsAbs(state) {
		home = path.Clean(state)
	}
	if !path.IsAbs(home) {
		return fmt.Errorf("the daemon's state home %q must be an absolute path", home)
	}
	w.places[dir] = home
	return nil
}

func (w *jobToolWorld) start(dir, build string, sandbox, told bool, getenv envcodec.Lookup) (*worldDaemon, error) {
	if _, recorded := w.places[dir]; !recorded {
		if told {
			return nil, fmt.Errorf("the runtime directory %s does not record where its daemon keeps state; whoever chose it sets it up again with `daemon ensure`", dir)
		}
		if err := w.record(dir, getenv); err != nil {
			return nil, err
		}
	}
	if !sandbox {
		if w.holder != "" && w.holder != dir {
			return nil, fmt.Errorf("another vibesys-cli daemon of this user holds this machine: runtime directory %s", w.holder)
		}
		w.holder = dir
	}
	w.generations++
	service := clidaemontest.New()
	service.SetFacts(w.stores[w.places[dir]])
	d := &worldDaemon{Dir: dir, Build: build, StateHome: w.places[dir], Sandbox: sandbox, Generation: w.generations, service: service}
	w.daemons[dir] = d
	return d, nil
}

// Connect implements clidaemon.Connector over the sockets the World answers.
func (w *jobToolWorld) Connect(_ context.Context, socket clidaemon.Socket) (clidaemon.Connection, error) {
	dir := path.Dir(socket.Path())
	d := w.daemons[dir]
	if d == nil || w.unreachable[dir] {
		return nil, &clidaemon.Unreadable{Reason: core.NoDaemon, Detail: "connection refused: " + socket.Path()}
	}
	return d.service, nil
}

// Daemon is the daemon of a runtime directory, or nil.
func (w *jobToolWorld) Daemon(dir string) *worldDaemon { return w.daemons[dir] }

// Daemons is every daemon that runs, by runtime directory.
func (w *jobToolWorld) Daemons() []*worldDaemon {
	var out []*worldDaemon
	for _, d := range w.daemons {
		out = append(out, d)
	}
	slices.SortFunc(out, func(a, b *worldDaemon) int { return strings.Compare(a.Dir, b.Dir) })
	return out
}

// Kill ends the daemon of a directory, however it ends: what it left behind makes no difference to a
// client, which finds nobody answering a stale socket as it finds no socket.
func (w *jobToolWorld) Kill(dir string) {
	if w.holder == dir {
		w.holder = ""
	}
	delete(w.daemons, dir)
}

// Wipe removes a runtime directory with everything in it, as a reboot or the end of a login does.
func (w *jobToolWorld) Wipe(dir string) {
	w.Kill(dir)
	delete(w.places, dir)
}

// SetUnreachable makes a directory's daemon hold the machine and answer nobody, or stop doing that.
func (w *jobToolWorld) SetUnreachable(dir string, unreachable bool) { w.unreachable[dir] = unreachable }

// Places is the state home a directory records, empty when it records none.
func (w *jobToolWorld) Places(dir string) string { return w.places[dir] }

// Store is the facts of a state home, which outlive any daemon.
func (w *jobToolWorld) Store(stateHome string) []*pb.TaskFact {
	out := make([]*pb.TaskFact, 0, len(w.stores[stateHome]))
	for _, f := range w.stores[stateHome] {
		out = append(out, proto.Clone(f).(*pb.TaskFact))
	}
	return out
}

// Stores is every state home that holds anything.
func (w *jobToolWorld) Stores() []string {
	var out []string
	for home := range w.stores {
		out = append(out, home)
	}
	slices.Sort(out)
	return out
}
