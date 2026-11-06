package clientcli_test

import (
	"context"
	"errors"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/fstest"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// fakeConfigs is the configuration in a root, in memory. A set err is a file that does not read.
type fakeConfigs struct {
	config *pb.HostConfig
	err    error
	loads  int
	// refuseSave is what Save says of a configuration no host could use.
	refuseSave error
}

func (f *fakeConfigs) Load(string) (*pb.HostConfig, error) {
	f.loads++
	if f.err != nil {
		return nil, f.err
	}
	if f.config == nil {
		return &pb.HostConfig{}, nil
	}
	return f.config, nil
}

func (f *fakeConfigs) Save(_ string, config *pb.HostConfig) error {
	if f.refuseSave != nil {
		return f.refuseSave
	}
	f.config, f.err = config, nil
	return nil
}

// allEvidence is every kind of thing a client can gather about a host, crossed with every value of
// what decides anything about it.
func allEvidence() []clientcli.Evidence {
	const clientRelease, build, digest = "1.0.0", "this-build", "root-digest"
	var all []clientcli.Evidence
	for _, holder := range []*pb.HostLock{nil, {Pid: 7, Release: "1.0.0"}, {Pid: 7, Release: "1.0.0", Machine: "host-a"}, {Pid: 7, Release: "1.0.0", Machine: "host-b"}} {
		all = append(all,
			clientcli.Evidence{Holder: holder, Machine: "host-a", ClientRelease: clientRelease},
			clientcli.Evidence{Silent: true, Holder: holder, Machine: "host-a", ClientRelease: clientRelease})
	}
	unserved := []*pb.Unserved{
		nil,
		{Reason: pb.UnservedReason_UNSERVED_REASON_HELD_ELSEWHERE, Detail: "d"},
		{Reason: pb.UnservedReason_UNSERVED_REASON_HOLDER_GONE, Detail: "d"},
		{Reason: pb.UnservedReason_UNSERVED_REASON_STATE_REFUSED, Detail: "d"},
		{Reason: pb.UnservedReason_UNSERVED_REASON_CONFIG_REFUSED, Detail: "d"},
	}
	relations := []pb.ReleaseRelation{
		pb.ReleaseRelation_RELEASE_RELATION_SAME, pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER,
		pb.ReleaseRelation_RELEASE_RELATION_HOST_NEWER, pb.ReleaseRelation_RELEASE_RELATION_UNKNOWN,
		pb.ReleaseRelation_RELEASE_RELATION_UNSPECIFIED,
	}
	for _, why := range unserved {
		for _, relation := range relations {
			for _, hostRelease := range []string{clientRelease, "dev"} {
				for _, hostBuild := range []string{"", build, "another-build"} {
					for _, configOfHost := range []string{digest, "another-digest"} {
						for _, unreadable := range []bool{false, true} {
							for _, live := range []uint32{0, 1, 1 << 31} {
								for _, clients := range []uint32{0, 1, 9} {
									id := &pb.IdentifyResponse{Release: hostRelease, Relation: relation, BuildId: hostBuild, ConfigDigest: configOfHost, LiveRuns: live, ConnectedClients: clients}
									if why != nil {
										id.Orchestrator = &pb.IdentifyResponse_Unserved{Unserved: why}
									} else {
										id.Orchestrator = &pb.IdentifyResponse_Serving{Serving: &pb.Serving{}}
									}
									all = append(all, clientcli.Evidence{Identity: id, Machine: "host-a", ClientRelease: clientRelease, Build: build, ConfigDigest: digest, ConfigUnreadable: unreadable})
								}
							}
						}
					}
				}
			}
		}
	}
	return all
}

// Over every combination of what is found, the verb, the relation, connected clients and the machine,
// the policy has exactly one action, with the exit code that action means, and it holds to what a client
// must never do. This is the whole of the policy's promise; the cases below are its named instances.
func TestThePolicyHasOneOutcomeAndExitCodeForEveryInput(t *testing.T) {
	wantExit := map[clientcli.Action]int{
		clientcli.ActionUse: clientcli.ExitOK, clientcli.ActionStart: clientcli.ExitOK, clientcli.ActionReplace: clientcli.ExitOK,
		clientcli.ActionStop: clientcli.ExitOK, clientcli.ActionDone: clientcli.ExitOK,
		clientcli.ActionRefuse: clientcli.ExitFailed, clientcli.ActionUnavailable: clientcli.ExitUnavailable,
	}
	for _, evidence := range allEvidence() {
		r := clientcli.Classify(evidence)
		if again := clientcli.Classify(evidence); again != r {
			t.Fatalf("%+v: read as %+v and %+v", evidence, r, again)
		}
		for _, verb := range clientcli.Verbs {
			got := clientcli.Decide(r, verb)
			if again := clientcli.Decide(r, verb); again != got {
				t.Errorf("%+v, verb %d: %v and %v", r, verb, got, again)
			}
			exit, ok := wantExit[got.Action]
			if !ok || got.Exit != exit {
				t.Errorf("%+v, verb %d: %+v, want exit %d", r, verb, got, exit)
			}
			checkNeverDoes(t, r, verb, got.Action)
		}
	}
}

func checkNeverDoes(t *testing.T, r clientcli.Reading, verb clientcli.Verb, action clientcli.Action) {
	t.Helper()
	never := func(rule string, broken bool) {
		t.Helper()
		if broken {
			t.Errorf("%+v, verb %d, action %d: %s", r, verb, action, rule)
		}
	}
	replaces := action == clientcli.ActionReplace
	never("only a stop verb stops a host", action == clientcli.ActionStop && verb != clientcli.VerbStop)
	never("a read never replaces a host", replaces && verb == clientcli.VerbRead)
	never("only run start and host restart replace", replaces && verb != clientcli.VerbStart && verb != clientcli.VerbRestart)
	never("a host with live runs is never replaced", replaces && r.LiveRuns > 0)
	never("a host with other clients is not replaced unasked", replaces && r.Clients > 0 && verb != clientcli.VerbRestart)
	never("a newer host is not replaced unasked", replaces && r.Relation == clientcli.RelationHostNewer && verb != clientcli.VerbRestart)
	never("a configuration that does not read cannot replace unasked", replaces && r.Config == clientcli.ConfigUnreadable && verb != clientcli.VerbRestart)
	never("a host elsewhere is left alone", r.Found == clientcli.FoundElsewhere && action != clientcli.ActionUnavailable)
	never("a host that does not answer is left alone", r.Found == clientcli.FoundSilent && action != clientcli.ActionUnavailable)
	never("a host held elsewhere is not replaced by one that would be refused", r.Found == clientcli.FoundHeld && replaces)
	never("a host is started only where there is none", action == clientcli.ActionStart && r.Found != clientcli.FoundNothing)
	never("a read of a host that serves goes on, whatever it is", verb == clientcli.VerbRead && r.Found == clientcli.FoundServing && action != clientcli.ActionUse)
}

// The named rows of the decision table. The PR's table is this one.
func TestEachFindingEndsWithItsOwnAction(t *testing.T) {
	var (
		use         = clientcli.Decision{Action: clientcli.ActionUse, Exit: clientcli.ExitOK}
		start       = clientcli.Decision{Action: clientcli.ActionStart, Exit: clientcli.ExitOK}
		replace     = clientcli.Decision{Action: clientcli.ActionReplace, Exit: clientcli.ExitOK}
		stop        = clientcli.Decision{Action: clientcli.ActionStop, Exit: clientcli.ExitOK}
		done        = clientcli.Decision{Action: clientcli.ActionDone, Exit: clientcli.ExitOK}
		refuse      = clientcli.Decision{Action: clientcli.ActionRefuse, Exit: clientcli.ExitFailed}
		unavailable = clientcli.Decision{Action: clientcli.ActionUnavailable, Exit: clientcli.ExitUnavailable}
	)
	serving := func(relation clientcli.Relation, config clientcli.ConfigState, live, clients uint32) clientcli.Reading {
		return clientcli.Reading{Found: clientcli.FoundServing, Relation: relation, Config: config, LiveRuns: live, Clients: clients}
	}
	for _, c := range []struct {
		name string
		r    clientcli.Reading
		verb clientcli.Verb
		want clientcli.Decision
	}{
		{"nothing, read: start", clientcli.Reading{}, clientcli.VerbRead, start},
		{"nothing, run start: start", clientcli.Reading{}, clientcli.VerbStart, start},
		{"nothing, restart: start", clientcli.Reading{}, clientcli.VerbRestart, start},
		{"nothing, stop: nothing to do", clientcli.Reading{}, clientcli.VerbStop, done},

		{"elsewhere, read", clientcli.Reading{Found: clientcli.FoundElsewhere}, clientcli.VerbRead, unavailable},
		{"elsewhere, run start", clientcli.Reading{Found: clientcli.FoundElsewhere}, clientcli.VerbStart, unavailable},
		{"elsewhere, restart", clientcli.Reading{Found: clientcli.FoundElsewhere}, clientcli.VerbRestart, unavailable},
		{"elsewhere, stop", clientcli.Reading{Found: clientcli.FoundElsewhere}, clientcli.VerbStop, unavailable},

		{"silent, read", clientcli.Reading{Found: clientcli.FoundSilent}, clientcli.VerbRead, unavailable},
		{"silent, run start", clientcli.Reading{Found: clientcli.FoundSilent}, clientcli.VerbStart, unavailable},
		{"silent, stop: --force is the way out", clientcli.Reading{Found: clientcli.FoundSilent}, clientcli.VerbStop, unavailable},

		{"same, read", serving(clientcli.RelationSame, clientcli.ConfigMatches, 0, 0), clientcli.VerbRead, use},
		{"same, run start", serving(clientcli.RelationSame, clientcli.ConfigMatches, 3, 3), clientcli.VerbStart, use},
		{"same, configuration unreadable, read", serving(clientcli.RelationSame, clientcli.ConfigUnreadable, 0, 0), clientcli.VerbRead, use},
		{"same, configuration unreadable, run start", serving(clientcli.RelationSame, clientcli.ConfigUnreadable, 0, 0), clientcli.VerbStart, use},
		{"same, configuration changed, read", serving(clientcli.RelationSame, clientcli.ConfigDiffers, 0, 0), clientcli.VerbRead, use},
		{"same, configuration changed, run start", serving(clientcli.RelationSame, clientcli.ConfigDiffers, 0, 0), clientcli.VerbStart, replace},
		{"same, configuration changed, runs live", serving(clientcli.RelationSame, clientcli.ConfigDiffers, 2, 0), clientcli.VerbStart, refuse},
		{"same, configuration changed, clients", serving(clientcli.RelationSame, clientcli.ConfigDiffers, 0, 2), clientcli.VerbStart, refuse},

		{"older, idle, read", serving(clientcli.RelationHostOlder, clientcli.ConfigMatches, 0, 0), clientcli.VerbRead, use},
		{"older, idle, run start", serving(clientcli.RelationHostOlder, clientcli.ConfigMatches, 0, 0), clientcli.VerbStart, replace},
		{"older, runs live, run start", serving(clientcli.RelationHostOlder, clientcli.ConfigMatches, 1, 0), clientcli.VerbStart, refuse},
		{"older, clients, run start", serving(clientcli.RelationHostOlder, clientcli.ConfigMatches, 0, 1), clientcli.VerbStart, refuse},
		{"older, configuration unreadable, run start", serving(clientcli.RelationHostOlder, clientcli.ConfigUnreadable, 0, 0), clientcli.VerbStart, refuse},

		{"another build, idle, read", serving(clientcli.RelationUnordered, clientcli.ConfigMatches, 0, 0), clientcli.VerbRead, use},
		{"another build, idle, run start", serving(clientcli.RelationUnordered, clientcli.ConfigMatches, 0, 0), clientcli.VerbStart, replace},
		{"another build, clients, run start", serving(clientcli.RelationUnordered, clientcli.ConfigMatches, 0, 1), clientcli.VerbStart, refuse},

		{"newer, idle, read", serving(clientcli.RelationHostNewer, clientcli.ConfigMatches, 0, 0), clientcli.VerbRead, use},
		{"newer, idle, run start: used", serving(clientcli.RelationHostNewer, clientcli.ConfigMatches, 0, 0), clientcli.VerbStart, use},
		{"newer, runs live, run start: used", serving(clientcli.RelationHostNewer, clientcli.ConfigMatches, 2, 2), clientcli.VerbStart, use},
		{"newer, configuration changed, run start", serving(clientcli.RelationHostNewer, clientcli.ConfigDiffers, 0, 0), clientcli.VerbStart, refuse},
		{"newer, idle, restart", serving(clientcli.RelationHostNewer, clientcli.ConfigMatches, 0, 0), clientcli.VerbRestart, replace},

		{"restart, idle", serving(clientcli.RelationSame, clientcli.ConfigMatches, 0, 0), clientcli.VerbRestart, replace},
		{"restart, clients", serving(clientcli.RelationSame, clientcli.ConfigMatches, 0, 2), clientcli.VerbRestart, replace},
		{"restart, runs live", serving(clientcli.RelationSame, clientcli.ConfigMatches, 1, 0), clientcli.VerbRestart, refuse},
		{"stop, runs live", serving(clientcli.RelationSame, clientcli.ConfigMatches, 1, 0), clientcli.VerbStop, stop},

		{"held, read", clientcli.Reading{Found: clientcli.FoundHeld}, clientcli.VerbRead, unavailable},
		{"held, run start", clientcli.Reading{Found: clientcli.FoundHeld}, clientcli.VerbStart, unavailable},
		{"held, restart", clientcli.Reading{Found: clientcli.FoundHeld}, clientcli.VerbRestart, unavailable},
		{"held, stop", clientcli.Reading{Found: clientcli.FoundHeld}, clientcli.VerbStop, stop},
		{"holder gone, read", clientcli.Reading{Found: clientcli.FoundHolderGone}, clientcli.VerbRead, unavailable},
		{"holder gone, run start", clientcli.Reading{Found: clientcli.FoundHolderGone}, clientcli.VerbStart, replace},
		{"holder gone, restart", clientcli.Reading{Found: clientcli.FoundHolderGone}, clientcli.VerbRestart, replace},
		{"holder gone, clients, run start", clientcli.Reading{Found: clientcli.FoundHolderGone, Clients: 1}, clientcli.VerbStart, refuse},
		{"holder gone, newer, run start", clientcli.Reading{Found: clientcli.FoundHolderGone, Relation: clientcli.RelationHostNewer}, clientcli.VerbStart, refuse},
		{"state refused, read", clientcli.Reading{Found: clientcli.FoundStateRefused}, clientcli.VerbRead, unavailable},
		{"state refused, run start", clientcli.Reading{Found: clientcli.FoundStateRefused}, clientcli.VerbStart, replace},
	} {
		if got := clientcli.Decide(c.r, c.verb); got != c.want {
			t.Errorf("%s: %+v, want %+v", c.name, got, c.want)
		}
	}
}

// The relation comes from the host, for two releases. For a build of no release "dev" says nothing, so
// two of them are the same only when the host is the very program this client would start. The
// configuration is read apart from it.
func TestClassifyReadsTheRelationAndTheBuild(t *testing.T) {
	const digest = "the-root-says-this"
	for _, c := range []struct {
		name        string
		hostRelease string
		relation    pb.ReleaseRelation
		client      string
		hostBuild   string
		wouldStart  string
		want        clientcli.Relation
	}{
		{name: "same release", hostRelease: "1.0.1", relation: pb.ReleaseRelation_RELEASE_RELATION_SAME, client: "1.0.1", want: clientcli.RelationSame},
		{name: "host older", hostRelease: "1.0.0", relation: pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER, client: "1.0.1", want: clientcli.RelationHostOlder},
		{name: "host newer", hostRelease: "1.0.2", relation: pb.ReleaseRelation_RELEASE_RELATION_HOST_NEWER, client: "1.0.1", want: clientcli.RelationHostNewer},
		{name: "host of no release", hostRelease: "dev", relation: pb.ReleaseRelation_RELEASE_RELATION_UNKNOWN, hostBuild: "b", client: "1.0.1", wouldStart: "c", want: clientcli.RelationUnordered},
		{name: "client of no release", hostRelease: "1.0.1", relation: pb.ReleaseRelation_RELEASE_RELATION_UNKNOWN, client: "dev", wouldStart: "c", want: clientcli.RelationUnordered},
		{name: "the same build", hostRelease: "dev", relation: pb.ReleaseRelation_RELEASE_RELATION_UNKNOWN, hostBuild: "b", client: "dev", wouldStart: "b", want: clientcli.RelationSame},
		{name: "a rebuilt client", hostRelease: "dev", relation: pb.ReleaseRelation_RELEASE_RELATION_UNKNOWN, hostBuild: "b", client: "dev", wouldStart: "c", want: clientcli.RelationUnordered},
		{name: "no host found to compare", hostRelease: "dev", relation: pb.ReleaseRelation_RELEASE_RELATION_UNKNOWN, hostBuild: "b", client: "dev", want: clientcli.RelationUnordered},
		{name: "a host older than build ids", hostRelease: "dev", relation: pb.ReleaseRelation_RELEASE_RELATION_UNKNOWN, client: "dev", wouldStart: "", want: clientcli.RelationUnordered},
	} {
		id := &pb.IdentifyResponse{Release: c.hostRelease, Relation: c.relation, BuildId: c.hostBuild, ConfigDigest: digest, Orchestrator: &pb.IdentifyResponse_Serving{Serving: &pb.Serving{}}}
		got := clientcli.Classify(clientcli.Evidence{Identity: id, ClientRelease: c.client, Build: c.wouldStart, ConfigDigest: digest})
		if got.Relation != c.want || got.Found != clientcli.FoundServing || got.Config != clientcli.ConfigMatches {
			t.Errorf("%s: %+v, want relation %d", c.name, got, c.want)
		}
	}
	id := &pb.IdentifyResponse{Relation: pb.ReleaseRelation_RELEASE_RELATION_SAME, ConfigDigest: "older"}
	for _, c := range []struct {
		name       string
		root       string
		unreadable bool
		want       clientcli.ConfigState
	}{
		{"changed", "newer", false, clientcli.ConfigDiffers},
		{"the same", "older", false, clientcli.ConfigMatches},
		{"does not read", "", true, clientcli.ConfigUnreadable},
	} {
		if got := clientcli.Classify(clientcli.Evidence{Identity: id, ConfigDigest: c.root, ConfigUnreadable: c.unreadable}); got.Config != c.want {
			t.Errorf("%s: %+v", c.name, got)
		}
	}
}

// A record on another machine makes it a host elsewhere only when nobody answered here: an answer
// proves the host is reachable, whatever a stale record says. Without a name it is this machine's.
func TestClassifyTellsAHostElsewhereFromOneHere(t *testing.T) {
	for _, c := range []struct {
		name    string
		holder  *pb.HostLock
		machine string
		want    clientcli.Finding
	}{
		{"no holder", nil, "host-a", clientcli.FoundNothing},
		{"held here", &pb.HostLock{Pid: 1, Release: "1", Machine: "host-a"}, "host-a", clientcli.FoundNothing},
		{"held elsewhere", &pb.HostLock{Pid: 1, Release: "1", Machine: "host-b"}, "host-a", clientcli.FoundElsewhere},
		{"a record older than the name", &pb.HostLock{Pid: 1, Release: "1"}, "host-a", clientcli.FoundNothing},
		{"this machine has no name", &pb.HostLock{Pid: 1, Release: "1", Machine: "host-b"}, "", clientcli.FoundNothing},
	} {
		if got := clientcli.Classify(clientcli.Evidence{Holder: c.holder, Machine: c.machine}).Found; got != c.want {
			t.Errorf("%s: %d, want %d", c.name, got, c.want)
		}
	}
}

// startable is a host world whose Connect starts the host through start, which may refuse.
func startable(h *fakeHost, start func() error) clientcli.Env {
	env := hostWorld(h, nil)
	plain := env.Connect
	env.Connect = func(root string) (clientcli.Dial, error) {
		dial, err := plain(root)
		if err != nil {
			return nil, err
		}
		return func(ctx context.Context) (net.Conn, error) {
			if !h.isRunning() {
				if err := start(); err != nil {
					return nil, err
				}
			}
			return dial(ctx)
		}, nil
	}
	return env
}

// inRepository makes run start reach for the host.
func inRepository(env clientcli.Env) clientcli.Env {
	env.FS = fstest.MapFS{"work/repo/.git": {}}
	env.Dir = "/work/repo"
	env.Canonical = func(path string) string { return path }
	env.IDs = &counterIDs{}
	return env
}

func runStart(env clientcli.Env) (code int, stdout, stderr string) {
	return runHost(inRepository(env), "run", "start", "--task", "t", "--detach")
}

func older(live uint32) *pb.IdentifyResponse {
	id := identity(live)
	id.Release, id.Relation = "0.9.0", pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER
	return id
}

func newer(live uint32) *pb.IdentifyResponse {
	id := identity(live)
	id.Release, id.Relation = "1.1.0", pb.ReleaseRelation_RELEASE_RELATION_HOST_NEWER
	return id
}

// A launch on a host of another build with live runs is refused with exit 1, says what works whatever
// protocol that host speaks, and asks the host for nothing. The same for a host on another
// configuration.
func TestALaunchIsRefusedOnAMismatchedHostWithLiveRunsAndNoRunIsTouched(t *testing.T) {
	for name, make := range map[string]func() (*fakeHost, *fakeConfigs){
		"host older": func() (*fakeHost, *fakeConfigs) {
			return &fakeHost{running: true, identity: older(2)}, &fakeConfigs{}
		},
		"another configuration": func() (*fakeHost, *fakeConfigs) {
			return &fakeHost{running: true, identity: identity(2)}, &fakeConfigs{config: &pb.HostConfig{JobTool: "/x/vibesys-cli"}}
		},
	} {
		t.Run(name, func(t *testing.T) {
			h, configs := make()
			env := hostWorld(h, nil)
			env.Configs = configs
			code, _, errb := runStart(env)
			if code != clientcli.ExitFailed || !strings.Contains(errb, "never replaced under live runs") {
				t.Fatalf("exit %d: %s", code, errb)
			}
			// `run stop` needs the protocol to match, which on a host of another build it may not.
			if strings.Contains(errb, "run stop") || !strings.Contains(errb, "host stop") {
				t.Errorf("the advice is not what works on any host: %s", errb)
			}
			if len(h.stops) != 0 || h.starts != 0 || !h.isRunning() {
				t.Fatalf("the host was touched: stops %v, starts %d, running %v", h.stops, h.starts, h.isRunning())
			}
		})
	}
}

// An idle host that is the wrong one is asked to stop only if idle, is replaced, and the client says
// so on stderr, when the verb is run start.
func TestAnIdleMismatchedHostIsReplacedByARunStartAndNeverStoppedUnconditionally(t *testing.T) {
	for name, make := range map[string]func() (*fakeHost, *fakeConfigs){
		"host older": func() (*fakeHost, *fakeConfigs) {
			return &fakeHost{running: true, identity: older(0)}, &fakeConfigs{}
		},
		"another configuration": func() (*fakeHost, *fakeConfigs) {
			return &fakeHost{running: true, identity: identity(0)}, &fakeConfigs{config: &pb.HostConfig{JobTool: "/x/vibesys-cli"}}
		},
	} {
		t.Run(name, func(t *testing.T) {
			h, configs := make()
			env := hostWorld(h, nil)
			env.Configs = configs
			// The in-memory host serves no orchestrator, so the verb goes on to fail its handshake;
			// what matters here is everything before that.
			_, _, errb := runStart(env)
			if len(h.stops) != 1 || !h.stops[0] {
				t.Fatalf("stops %v, want one, only if idle", h.stops)
			}
			if h.starts != 1 || !strings.Contains(errb, "replaced the host") {
				t.Fatalf("starts %d: %s", h.starts, errb)
			}
		})
	}
}

// No read verb stops a host, of any release or configuration, with or without live runs: it goes on,
// with a note. Finding: a read verb replaced a working idle host.
func TestAReadNeverStopsAHostAndSaysWhatIsOdd(t *testing.T) {
	for name, make := range map[string]func() (*pb.IdentifyResponse, *fakeConfigs){
		"older, idle":         func() (*pb.IdentifyResponse, *fakeConfigs) { return older(0), &fakeConfigs{} },
		"older, live":         func() (*pb.IdentifyResponse, *fakeConfigs) { return older(1), &fakeConfigs{} },
		"newer, idle":         func() (*pb.IdentifyResponse, *fakeConfigs) { return newer(0), &fakeConfigs{} },
		"another build, idle": func() (*pb.IdentifyResponse, *fakeConfigs) { return anotherBuild(), &fakeConfigs{} },
		"another configuration": func() (*pb.IdentifyResponse, *fakeConfigs) {
			return identity(0), &fakeConfigs{config: &pb.HostConfig{JobTool: "/x"}}
		},
		"configuration is broken": func() (*pb.IdentifyResponse, *fakeConfigs) { return identity(0), &fakeConfigs{err: errors.New("x")} },
	} {
		for _, verb := range [][]string{{"run", "list"}, {"run", "get", "r"}, {"run", "attach", "r"}, {"run", "stop", "r", "--yes"}, {"run", "steer", "r", "hello"}, {"run", "wait", "r", "--until", "ended", "--timeout", "1m"}} {
			t.Run(name+"/"+verb[1], func(t *testing.T) {
				id, configs := make()
				h := &fakeHost{running: true, identity: id}
				env := hostWorld(h, nil)
				env.Configs = configs
				env.IDs = &counterIDs{}
				_, _, errb := runHost(env, verb...)
				if len(h.stops) != 0 || h.starts != 0 || !h.isRunning() {
					t.Fatalf("stops %v, starts %d, running %v: %s", h.stops, h.starts, h.isRunning(), errb)
				}
				if !strings.Contains(errb, "note:") && !strings.Contains(errb, "warning:") {
					t.Errorf("nothing was said: %s", errb)
				}
			})
		}
	}
}

// anotherBuild is a host of no release other than the one this client would start.
func anotherBuild() *pb.IdentifyResponse {
	id := identity(0)
	id.Release, id.Relation, id.BuildId = "1.0.0", pb.ReleaseRelation_RELEASE_RELATION_UNKNOWN, "an-old-build"
	return id
}

// A read verb reaches a host that is there without needing what it would take to start one: a build
// that cannot find the host program, or a configuration no host could start with.
func TestAReadOfARunningHostNeedsNothingAStartWould(t *testing.T) {
	h := &fakeHost{running: true, identity: identity(0)}
	env := hostWorld(h, nil)
	connected := 0
	env.Connect = func(string) (clientcli.Dial, error) {
		connected++
		return nil, errors.New("no vibesys-host next to this program or on PATH")
	}
	_, _, errb := runHost(env, "run", "list")
	if connected != 0 || strings.Contains(errb, "no vibesys-host") {
		t.Fatalf("connected %d: %s", connected, errb)
	}
}

// Finding: a client stopped a working host before finding it could not start a new one. Whatever makes
// the proof fail, no stop was asked, the host is up, and the failure says why and that it stayed.
func TestAHostIsNotAskedToStopUntilANewOneIsProvenAbleToStart(t *testing.T) {
	for _, verb := range [][]string{{"run", "start", "--task", "t", "--detach"}, {"host", "restart"}} {
		for name, proof := range map[string]struct {
			err  error
			code int
		}{
			"no host program":   {errors.New("no vibesys-host: not found"), clientcli.ExitUnavailable},
			"state unusable":    {&clientcli.CannotStart{Problem: "the state directory /x is not a directory"}, clientcli.ExitFailed},
			"release not whole": {&clientcli.CannotStart{Problem: "the release lacks bin/vibesys-cli"}, clientcli.ExitFailed},
			"configuration bad": {&clientcli.CannotStart{Problem: "host-config.json: unknown field \"x\""}, clientcli.ExitFailed},
		} {
			t.Run(verb[0]+"/"+name, func(t *testing.T) {
				h := &fakeHost{running: true, identity: older(0)}
				env := inRepository(hostWorld(h, nil))
				env.Prove = func(string) (clientcli.Dial, error) { return nil, proof.err }
				code, _, errb := runHost(env, verb...)
				if code != proof.code || !strings.Contains(errb, proof.err.Error()) || !strings.Contains(errb, "stays as it is") {
					t.Fatalf("exit %d: %s", code, errb)
				}
				if len(h.stops) != 0 || !h.isRunning() || h.starts != 0 {
					t.Fatalf("stops %v, running %v, starts %d", h.stops, h.isRunning(), h.starts)
				}
			})
		}
	}
}

// Finding: a bad configuration file blocked every verb. A host that is running goes on being used, with
// a warning that names the file; status reports it; --clear never reads it; and a verb that must start
// a host refuses, as Connect says.
func TestABadConfigurationIsAWarningUntilAHostMustStart(t *testing.T) {
	bad := errors.New(`/root/host-config.json: not a host configuration: unknown field "stateDirectry"`)
	for _, verb := range [][]string{{"run", "list"}, {"run", "start", "--task", "t", "--detach"}, {"host", "status"}} {
		t.Run("running/"+verb[1], func(t *testing.T) {
			h := &fakeHost{running: true, identity: identity(0)}
			env := inRepository(hostWorld(h, nil))
			env.Configs = &fakeConfigs{err: bad}
			_, _, errb := runHost(env, verb...)
			if !strings.Contains(errb, "warning:") || !strings.Contains(errb, "host-config.json") || !strings.Contains(errb, "stateDirectry") || !strings.Contains(errb, "--clear") {
				t.Fatalf("%s", errb)
			}
			if len(h.stops) != 0 || !h.isRunning() || h.starts != 0 {
				t.Fatalf("stops %v, running %v, starts %d", h.stops, h.isRunning(), h.starts)
			}
		})
	}

	h := &fakeHost{running: true, identity: identity(0)}
	env := hostWorld(h, nil)
	env.Configs = &fakeConfigs{err: bad}
	if code, out, _ := runHost(env, "host", "status"); code != clientcli.ExitOK || !strings.Contains(out, "release 1.0.0") {
		t.Fatalf("status of a healthy host: exit %d: %s", code, out)
	}
	if code, _, errb := runHost(env, "host", "configure"); code != clientcli.ExitFailed || !strings.Contains(errb, "stateDirectry") || !strings.Contains(errb, "--clear") {
		t.Fatalf("printing it: exit %d: %s", code, errb)
	}

	configs := &fakeConfigs{err: bad}
	env.Configs = configs
	if code, _, errb := runHost(env, "host", "configure", "--clear"); code != clientcli.ExitOK || configs.loads != 0 {
		t.Fatalf("--clear: exit %d, %d loads: %s", code, configs.loads, errb)
	}
	configs = &fakeConfigs{err: bad}
	env.Configs = configs
	if code, _, errb := runHost(env, "host", "configure", "--clear", "--job-tool", "/x/tool"); code != clientcli.ExitOK || configs.loads != 0 || configs.config.GetJobTool() != "/x/tool" {
		t.Fatalf("--clear with a flag: exit %d, %d loads: %s", code, configs.loads, errb)
	}

	none := &fakeHost{identity: identity(0)}
	env = hostWorld(none, nil)
	env.Configs = &fakeConfigs{err: bad}
	env.Connect = func(string) (clientcli.Dial, error) { return nil, &clientcli.CannotStart{Problem: bad.Error()} }
	if code, _, errb := runHost(env, "run", "list"); code != clientcli.ExitFailed || !strings.Contains(errb, "stateDirectry") || none.starts != 0 {
		t.Fatalf("no host: exit %d, started %d: %s", code, none.starts, errb)
	}
	if code, _, errb := runHost(env, "host", "status"); code != clientcli.ExitUnavailable || !strings.Contains(errb, "stateDirectry") {
		t.Fatalf("status with no host: exit %d: %s", code, errb)
	}
}

// host configure writes nothing that Save refuses, and says why.
func TestHostConfigureRefusesWhatSaveRefuses(t *testing.T) {
	configs := &fakeConfigs{refuseSave: errors.New("job_tool: /x is not an executable file")}
	env := hostWorld(&fakeHost{identity: identity(0)}, nil)
	env.Configs = configs
	code, _, errb := runHost(env, "host", "configure", "--job-tool", "/x")
	if code != clientcli.ExitFailed || !strings.Contains(errb, "job_tool") || configs.config != nil {
		t.Fatalf("exit %d, recorded %v: %s", code, configs.config, errb)
	}
}

// Finding: a newer host was replaced like an older one. It is used, by any verb, with a note, at any
// number of live runs, and stays up.
func TestANewerHostIsUsedAndNeverReplaced(t *testing.T) {
	for _, live := range []uint32{0, 3} {
		h := &fakeHost{running: true, identity: newer(live)}
		_, _, errb := runStart(hostWorld(h, nil))
		if len(h.stops) != 0 || h.starts != 0 || !strings.Contains(errb, "release 1.1.0") {
			t.Fatalf("%d live: stops %v, starts %d: %s", live, h.stops, h.starts, errb)
		}
	}
	h := &fakeHost{running: true, identity: newer(0)}
	env := hostWorld(h, nil)
	env.Configs = &fakeConfigs{config: &pb.HostConfig{JobTool: "/x"}}
	code, _, errb := runStart(env)
	if code != clientcli.ExitFailed || !strings.Contains(errb, "host restart") || len(h.stops) != 0 {
		t.Fatalf("on another configuration: exit %d, stops %v: %s", code, h.stops, errb)
	}
	// host restart is how an operator overrules it.
	h = &fakeHost{running: true, identity: newer(0)}
	if code, _, errb := runHost(hostWorld(h, nil), "host", "restart"); code != clientcli.ExitOK || len(h.stops) != 1 {
		t.Fatalf("restart: exit %d, stops %v: %s", code, h.stops, errb)
	}
}

// The handshake's own failure names host restart, in both directions.
func TestAProtocolMismatchNamesHostRestart(t *testing.T) {
	for name, compatibility := range map[string]pb.Compatibility{
		"host too old":   pb.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD,
		"client too old": pb.Compatibility_COMPATIBILITY_CLIENT_TOO_OLD,
	} {
		t.Run(name, func(t *testing.T) {
			f := &fakeService{hello: func() (*pb.HelloResponse, error) {
				return &pb.HelloResponse{Daemon: &pb.BuildVersions{BinaryVersion: "9.9", ProtocolVersion: 2, MinProtocolVersion: 2}, Compatibility: compatibility}, nil
			}}
			got := newWorld(t, serveFake(t, f)).run(context.Background(), "run", "list")
			contains(t, "stderr", got.stderr, "host restart")
		})
	}
}

// Finding: a rebuilt dev client kept an old host. Two builds of no release are told apart by which
// program they are: the same one is used, another is replaced by run start when idle.
func TestABuildOfNoReleaseIsToldApartByWhichProgramItIs(t *testing.T) {
	dev := func(build string) *pb.IdentifyResponse {
		id := identity(0)
		id.Release, id.Relation, id.BuildId = "dev", pb.ReleaseRelation_RELEASE_RELATION_UNKNOWN, build
		return id
	}
	world := func(h *fakeHost) clientcli.Env {
		env := hostWorld(h, nil)
		env.Version = "dev"
		env.HostBuild = func(string) string { return "this-build" }
		return env
	}
	same := &fakeHost{running: true, identity: dev("this-build")}
	if _, _, errb := runStart(world(same)); len(same.stops) != 0 || strings.Contains(errb, "so it stays") {
		t.Fatalf("the same build: stops %v: %s", same.stops, errb)
	}
	rebuilt := &fakeHost{running: true, identity: dev("an-older-build")}
	if _, _, errb := runStart(world(rebuilt)); len(rebuilt.stops) != 1 || rebuilt.starts != 1 || !strings.Contains(errb, "another build") {
		t.Fatalf("a rebuilt client: stops %v, starts %d: %s", rebuilt.stops, rebuilt.starts, errb)
	}
}

// Finding: an installed client replaced a dev host while its connections were open. A host with other
// clients is not idle, for run start, and the message names them and the way out.
func TestAHostWithConnectedClientsIsNotReplacedUnasked(t *testing.T) {
	id := anotherBuild()
	id.ConnectedClients = 2
	h := &fakeHost{running: true, identity: id}
	code, _, errb := runStart(hostWorld(h, nil))
	if code != clientcli.ExitFailed || !strings.Contains(errb, "2 other clients") || !strings.Contains(errb, "host restart") {
		t.Fatalf("exit %d: %s", code, errb)
	}
	if len(h.stops) != 0 || h.starts != 0 {
		t.Fatalf("stops %v, starts %d", h.stops, h.starts)
	}
	// Asked for by name, it goes, and says what that does.
	h = &fakeHost{running: true, identity: id}
	code, _, errb = runHost(hostWorld(h, nil), "host", "restart")
	if code != clientcli.ExitOK || len(h.stops) != 1 || !strings.Contains(errb, "lose their connection") {
		t.Fatalf("restart: exit %d, stops %v: %s", code, h.stops, errb)
	}
}

// A connection that has just closed is counted until the host has seen it close, so a client that left
// a moment ago does not keep a host from being replaced.
func TestAClientThatHasJustLeftDoesNotHoldAHost(t *testing.T) {
	id := anotherBuild()
	id.ConnectedClients = 1
	h := &fakeHost{running: true, identity: id, clientsLeaveAfter: 2}
	_, _, errb := runStart(hostWorld(h, nil))
	if len(h.stops) != 1 || h.starts != 1 {
		t.Fatalf("stops %v, starts %d: %s", h.stops, h.starts, errb)
	}
}

// A host that does not answer ends a verb with 4 and the way out, and nothing is started over it.
func TestAHostThatDoesNotAnswerIsUnavailableWithTheWayOut(t *testing.T) {
	h := &fakeHost{running: true, oldHost: true, identity: identity(0)}
	code, _, errb := runHost(hostWorld(h, nil), "run", "list")
	if code != clientcli.ExitUnavailable || !strings.Contains(errb, "host stop --force") || h.starts != 0 {
		t.Fatalf("exit %d, started %d: %s", code, h.starts, errb)
	}
}

func unserved(reason pb.UnservedReason, detail string) *pb.IdentifyResponse {
	id := identity(0)
	id.Orchestrator = &pb.IdentifyResponse_Unserved{Unserved: &pb.Unserved{Reason: reason, Detail: detail, HeldByRelease: "0.9.0"}}
	return id
}

// Finding: a host up and not serving had no answer. Each reason has one: reads exit 4 saying who holds
// the state and what to do, nothing is stopped; run start replaces it once the holder has gone, and not
// while it is there, since a new host would be refused the same way.
func TestAHostThatDoesNotServeHasAnAnswerForEachReason(t *testing.T) {
	const holderWords = "held by pid 77, release 0.9.0"
	for _, c := range []struct {
		name          string
		reason        pb.UnservedReason
		startReplaces bool
		says          string
	}{
		{"held", pb.UnservedReason_UNSERVED_REASON_HELD_ELSEWHERE, false, "host stop"},
		{"holder gone", pb.UnservedReason_UNSERVED_REASON_HOLDER_GONE, true, "host restart"},
		{"state refused", pb.UnservedReason_UNSERVED_REASON_STATE_REFUSED, true, "host restart"},
	} {
		t.Run(c.name, func(t *testing.T) {
			h := &fakeHost{running: true, identity: unserved(c.reason, holderWords)}
			code, _, errb := runHost(hostWorld(h, nil), "run", "list")
			if code != clientcli.ExitUnavailable || !strings.Contains(errb, holderWords) || !strings.Contains(errb, c.says) {
				t.Fatalf("read: exit %d: %s", code, errb)
			}
			if len(h.stops) != 0 || h.starts != 0 {
				t.Fatalf("read: stops %v, starts %d", h.stops, h.starts)
			}
			_, out, _ := runHost(hostWorld(h, nil), "host", "status")
			if !strings.Contains(out, holderWords) || !strings.Contains(out, c.says) {
				t.Fatalf("status: %s", out)
			}

			h = &fakeHost{running: true, identity: unserved(c.reason, holderWords)}
			code, _, errb = runStart(hostWorld(h, nil))
			if c.startReplaces {
				if len(h.stops) != 1 || h.starts != 1 {
					t.Fatalf("start: stops %v, starts %d: %s", h.stops, h.starts, errb)
				}
				return
			}
			if code != clientcli.ExitUnavailable || len(h.stops) != 0 || h.starts != 0 || !strings.Contains(errb, holderWords) {
				t.Fatalf("start: exit %d, stops %v, starts %d: %s", code, h.stops, h.starts, errb)
			}
		})
	}
}

// wedgedHost answers Identify, and takes Stop and never answers it until the call goes away. entered is
// closed once Stop has arrived.
type wedgedHost struct {
	*fakeHost
	entered chan struct{}
	once    sync.Once
}

func (h *wedgedHost) Stop(ctx context.Context, _ *pb.StopRequest) (*pb.StopResponse, error) {
	h.once.Do(func() { close(h.entered) })
	<-ctx.Done()
	return nil, ctx.Err()
}

// fireOn is a clock whose ask-long timers end when gate closes, and all others never.
type fireOn struct {
	pacedClock
	gate <-chan struct{}
}

func (c fireOn) After(d time.Duration) <-chan time.Time {
	if d <= time.Second {
		return c.pacedClock.After(d)
	}
	ch := make(chan time.Time, 1)
	go func() {
		<-c.gate
		ch <- time.Time{}
	}()
	return ch
}

// Finding: a stop that a wedged host never answers read its own deadline as the host closing the
// connection, and polled for a host that would not go. Now that is a host that did not answer: no polling
// at all, exit 4, and the way out named.
func TestHostStopOnAWedgedHostSaysSoAtOnceAndNamesForce(t *testing.T) {
	h := &fakeHost{running: true, identity: identity(0)}
	wedged := &wedgedHost{fakeHost: h, entered: make(chan struct{})}
	h.control = wedged
	procs := &fakeProcesses{holder: &pb.HostLock{Pid: 4242, Release: "1.0.0"}, host: h}
	env := hostWorld(h, procs)
	env.Clock = fireOn{gate: wedged.entered}
	code, _, errb := runHost(env, "host", "stop")
	if code != clientcli.ExitUnavailable || !strings.Contains(errb, "did not answer") || !strings.Contains(errb, "--force") {
		t.Fatalf("exit %d: %s", code, errb)
	}
	if procs.polls != 0 {
		t.Fatalf("it waited for a host that was never told to stop: %d polls of the lock", procs.polls)
	}
	if len(procs.terminated) != 0 {
		t.Fatalf("no --force, and it ended %v", procs.terminated)
	}
}

// Findings 5 and 7's opposite: a host that closes the connection as it stops, before its reply is read,
// is still a stop.
func TestAHostThatClosesTheConnectionAsItStopsHasStopped(t *testing.T) {
	h := &fakeHost{running: true, identity: identity(0), closeOnStop: true}
	code, out, errb := runHost(hostWorld(h, nil), "host", "stop")
	if code != clientcli.ExitOK || !strings.Contains(out, "stopped") {
		t.Fatalf("exit %d: %s%s", code, out, errb)
	}
}

// Finding: from another machine on a shared home, nothing can reach the host, a start spun out its 10
// s, and --force signalled a number that means another program here. Every verb now refuses at once
// naming that machine, and no process is signalled.
func TestAHostOnAnotherMachineIsNamedAndLeftAlone(t *testing.T) {
	for _, verb := range [][]string{
		{"run", "list"}, {"run", "start", "--task", "t", "--detach"}, {"host", "status"}, {"host", "stop"},
		{"host", "stop", "--force"}, {"host", "stop", "--if-no-live-runs"}, {"host", "restart"},
	} {
		t.Run(strings.Join(verb, " "), func(t *testing.T) {
			h := &fakeHost{identity: identity(0)}
			procs := &fakeProcesses{holder: &pb.HostLock{Pid: 4242, Release: "1.0.0", Machine: "host-b"}}
			env := inRepository(hostWorld(h, procs))
			connected := 0
			env.Connect = func(string) (clientcli.Dial, error) { connected++; return nil, errors.New("reached for a start") }
			env.Prove = env.Connect
			code, _, errb := runHost(env, verb...)
			if code != clientcli.ExitUnavailable || !strings.Contains(errb, "host-b") || !strings.Contains(errb, "--root") {
				t.Fatalf("exit %d: %s", code, errb)
			}
			if connected != 0 || h.starts != 0 || len(procs.terminated) != 0 {
				t.Fatalf("connected %d, started %d, ended %v", connected, h.starts, procs.terminated)
			}
		})
	}
}

// A host that stopped answering and was found through its lock is ended only when the lock is this
// machine's: a stale connection failure must not reach a number on another machine either.
func TestForceNeverSignalsAProcessOnAnotherMachine(t *testing.T) {
	old := &fakeHost{running: true, oldHost: true, identity: identity(0)}
	procs := &fakeProcesses{holder: &pb.HostLock{Pid: 4242, Release: "0.1.0", Machine: "host-b"}, host: old}
	code, _, errb := runHost(hostWorld(old, procs), "host", "stop", "--force")
	if code != clientcli.ExitUnavailable || len(procs.terminated) != 0 || !strings.Contains(errb, "host-b") {
		t.Fatalf("exit %d, ended %v: %s", code, procs.terminated, errb)
	}
}

// A refused start ends with exit 1 and only what the start said; one that took too long ends with
// exit 3. A verb that found no host gets exactly one attempt.
func TestAFailedStartHasItsOwnExitCode(t *testing.T) {
	for name, c := range map[string]struct {
		err  error
		code int
		said string
	}{
		"refused": {&clientcli.StartRefused{Detail: "the state directory is on nfs"}, clientcli.ExitFailed, "the state directory is on nfs"},
		"late":    {&clientcli.StartTimedOut{Err: errors.New("deadline exceeded")}, clientcli.ExitTimeout, "did not come up in time"},
		"other":   {errors.New("cannot run it"), clientcli.ExitUnavailable, "cannot run it"},
	} {
		t.Run(name, func(t *testing.T) {
			h := &fakeHost{identity: identity(0)}
			attempts := 0
			env := startable(h, func() error { attempts++; return c.err })
			code, _, errb := runHost(env, "run", "list")
			if code != c.code || !strings.Contains(errb, c.said) || attempts != 1 {
				t.Fatalf("exit %d, %d attempts: %s", code, attempts, errb)
			}
		})
	}
}

// host configure writes what a flag gives, keeps the rest, prints with no flag, clears, and a value
// the message refuses is a refusal, with nothing kept. It starts no host.
func TestHostConfigureRecordsWhatTheNextHostStartsWith(t *testing.T) {
	h := &fakeHost{identity: identity(0)}
	configs := &fakeConfigs{}
	env := hostWorld(h, nil)
	env.Configs = configs
	if code, _, errb := runHost(env, "host", "configure", "--state-directory", "/local/state", "--tool-path", "/a:/b"); code != clientcli.ExitOK {
		t.Fatalf("exit %d: %s", code, errb)
	}
	if configs.config.GetStateDirectory() != "/local/state" || strings.Join(configs.config.GetToolPath(), ",") != "/a,/b" {
		t.Fatalf("recorded %v", configs.config)
	}
	if code, _, _ := runHost(env, "host", "configure", "--job-tool", "/x/tool"); code != clientcli.ExitOK || configs.config.GetStateDirectory() != "/local/state" || configs.config.GetJobTool() != "/x/tool" {
		t.Fatalf("exit %d: recorded %v, want the new field beside the old", code, configs.config)
	}
	code, out, _ := runHost(env, "host", "configure")
	if code != clientcli.ExitOK || !strings.Contains(out, "/local/state") {
		t.Fatalf("no flag: exit %d, %s", code, out)
	}
	if code, _, _ := runHost(env, "host", "configure", "--clear"); code != clientcli.ExitOK || configs.config.GetStateDirectory() != "" {
		t.Fatalf("clear: exit %d, %v", code, configs.config)
	}
	if h.starts != 0 {
		t.Fatalf("configure started a host")
	}
	if code, _, _ := runHost(env, "host", "configure", "nope"); code != clientcli.ExitUsage {
		t.Fatalf("a positional: exit %d", code)
	}
}

// fireUntilDone keeps ending every timer a verb starts until the verb is over, so that the first bound
// it comes to expires, whichever that is.
func fireUntilDone(w *world, done <-chan result) result {
	for {
		select {
		case got := <-done:
			return got
		case <-w.clock.armed:
			w.clock.fireAll()
		}
	}
}

// Finding: the handshake, Dispatch, Snapshot and the start of the stream ran on the caller's context,
// so a host that took a call and never answered hung the client for ever. Each has a bound, which ends
// in exit 3 and what to do about it.
func TestEveryCallToTheServiceHasABound(t *testing.T) {
	never := make(chan struct{})
	t.Cleanup(func() { close(never) })
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	frames := []*pb.WatchRunsResponse{snapshotFrame(1, false, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)})}
	for _, c := range []struct {
		name    string
		service *fakeService
		args    []string
	}{
		{"the handshake", &fakeService{hello: func() (*pb.HelloResponse, error) { <-never; return nil, errors.New("gone") }}, []string{"run", "list"}},
		{"the snapshot of a list", &fakeService{}, []string{"run", "list"}},
		{"the snapshot of a get", &fakeService{}, []string{"run", "get", "run-1"}},
		{"a dispatch", &fakeService{frames: frames, dispatch: func(*pb.Command) (*pb.DispatchResponse, error) { <-never; return nil, errors.New("gone") }}, []string{"run", "stop", "run-1", "--yes"}},
		{"the dispatch of a start", &fakeService{dispatch: func(*pb.Command) (*pb.DispatchResponse, error) { <-never; return nil, errors.New("gone") }}, []string{"run", "start", "--task", "t", "--detach"}},
	} {
		t.Run(c.name, func(t *testing.T) {
			w := newWorld(t, serveFake(t, c.service))
			done := make(chan result, 1)
			go func() { done <- w.run(context.Background(), c.args...) }()
			got := fireUntilDone(w, done)
			if got.code != clientcli.ExitTimeout {
				t.Fatalf("exit %d, want 3: %s", got.code, got.stderr)
			}
			contains(t, "stderr", got.stderr, "did not", "within", "host status")
		})
	}
}

// The bound on starting the stream is on starting it. Once the snapshot is in, a stream lasts as long
// as the run: firing the clock then ends nothing.
func TestTheBoundOnAStreamEndsWithItsSnapshot(t *testing.T) {
	var fired atomic.Bool
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	f := &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(1, false, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)})}}
	w := newWorld(t, serveFake(t, f))
	ctx, cancel := context.WithCancel(context.Background())
	began := make(chan struct{})
	var once sync.Once
	w.onStderr = func(written string) {
		if strings.Contains(written, "attached to run") {
			once.Do(func() { close(began) })
		}
	}
	done := make(chan result, 1)
	go func() { done <- w.run(ctx, "run", "attach", "run-1") }()
	select {
	case <-began:
	case got := <-done:
		t.Fatalf("it ended before it attached: %+v", got)
	case <-time.After(guard):
		t.Fatal("never attached")
	}
	w.clock.fireAll()
	fired.Store(true)
	select {
	case got := <-done:
		t.Fatalf("the clock ended a stream that was going: %+v", got)
	default:
	}
	cancel()
	got := <-done
	if got.code != clientcli.ExitOK {
		t.Fatalf("detaching: exit %d: %s", got.code, got.stderr)
	}
}
