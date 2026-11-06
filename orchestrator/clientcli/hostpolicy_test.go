package clientcli_test

import (
	"context"
	"errors"
	"net"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// fakeConfigs is the configuration in a root, in memory.
type fakeConfigs struct{ config *pb.HostConfig }

func (f *fakeConfigs) Load(string) (*pb.HostConfig, error) {
	if f.config == nil {
		return &pb.HostConfig{}, nil
	}
	return f.config, nil
}

func (f *fakeConfigs) Save(_ string, config *pb.HostConfig) error {
	f.config = config
	return nil
}

// Over every input the policy has exactly one outcome, with the exit code that outcome means: a verb
// that goes on has nothing to exit with, a refusal is 1 and a host that cannot be reached is 4. It
// never replaces a host with live runs, under any input, and never refuses what it could serve.
func TestThePolicyHasOneOutcomeAndExitCodeForEveryInput(t *testing.T) {
	wantExit := map[clientcli.Action]int{
		clientcli.ActionUse: clientcli.ExitOK, clientcli.ActionStart: clientcli.ExitOK, clientcli.ActionReplace: clientcli.ExitOK,
		clientcli.ActionRefuse: clientcli.ExitFailed, clientcli.ActionUnavailable: clientcli.ExitUnavailable,
	}
	for _, found := range clientcli.Findings {
		for _, live := range []uint32{0, 1, 2, 7, 1 << 31} {
			for _, need := range []clientcli.Need{clientcli.NeedService, clientcli.NeedLaunch} {
				got := clientcli.Decide(found, live, need)
				if again := clientcli.Decide(found, live, need); again != got {
					t.Errorf("found %d, %d live, need %d: %v and %v", found, live, need, got, again)
				}
				exit, ok := wantExit[got.Action]
				if !ok || got.Exit != exit {
					t.Errorf("found %d, %d live, need %d: %+v, want exit %d", found, live, need, got, exit)
				}
				if got.Action == clientcli.ActionReplace && live != 0 {
					t.Errorf("found %d, %d live, need %d: a host with live runs would be replaced", found, live, need)
				}
				if got.Action == clientcli.ActionRefuse && need != clientcli.NeedLaunch {
					t.Errorf("found %d, %d live, need %d: only a launch is ever refused", found, live, need)
				}
			}
		}
	}
}

// One case per value of the closed set, with and without live runs.
func TestEachFindingEndsWithItsOwnAction(t *testing.T) {
	for _, c := range []struct {
		name  string
		found clientcli.Finding
		live  uint32
		need  clientcli.Need
		want  clientcli.Decision
	}{
		{"nothing there: start", clientcli.FoundNothing, 0, clientcli.NeedLaunch, clientcli.Decision{Action: clientcli.ActionStart, Exit: clientcli.ExitOK}},
		{"compatible: use", clientcli.FoundCompatible, 3, clientcli.NeedLaunch, clientcli.Decision{Action: clientcli.ActionUse, Exit: clientcli.ExitOK}},
		{"incompatible and idle: replace", clientcli.FoundIncompatible, 0, clientcli.NeedService, clientcli.Decision{Action: clientcli.ActionReplace, Exit: clientcli.ExitOK}},
		{"incompatible with runs, reading: use", clientcli.FoundIncompatible, 1, clientcli.NeedService, clientcli.Decision{Action: clientcli.ActionUse, Exit: clientcli.ExitOK}},
		{"incompatible with runs, launching: refuse", clientcli.FoundIncompatible, 1, clientcli.NeedLaunch, clientcli.Decision{Action: clientcli.ActionRefuse, Exit: clientcli.ExitFailed}},
		{"silent: unavailable", clientcli.FoundSilent, 0, clientcli.NeedLaunch, clientcli.Decision{Action: clientcli.ActionUnavailable, Exit: clientcli.ExitUnavailable}},
		{"another configuration and idle: replace", clientcli.FoundConfigMismatch, 0, clientcli.NeedLaunch, clientcli.Decision{Action: clientcli.ActionReplace, Exit: clientcli.ExitOK}},
		{"another configuration with runs, reading: use", clientcli.FoundConfigMismatch, 2, clientcli.NeedService, clientcli.Decision{Action: clientcli.ActionUse, Exit: clientcli.ExitOK}},
		{"another configuration with runs, launching: refuse", clientcli.FoundConfigMismatch, 2, clientcli.NeedLaunch, clientcli.Decision{Action: clientcli.ActionRefuse, Exit: clientcli.ExitFailed}},
	} {
		if got := clientcli.Decide(c.found, c.live, c.need); got != c.want {
			t.Errorf("%s: %+v, want %+v", c.name, got, c.want)
		}
	}
}

// Both directions of the release mismatch are incompatible, and so is a host of no release under a
// client of one and the reverse. A release beats a configuration mismatch, which replaces the host
// either way; two builds of no release cannot be told apart, so they are the same.
func TestClassifyReadsBothDirectionsOfEachMismatch(t *testing.T) {
	const digest = "the-root-says-this"
	for _, c := range []struct {
		name         string
		hostRelease  string
		relation     pb.ReleaseRelation
		client       string
		hostDigest   string
		want         clientcli.Finding
		configOfRoot string
	}{
		{name: "host older", hostRelease: "1.0.0", relation: pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER, client: "1.0.1", hostDigest: digest, configOfRoot: digest, want: clientcli.FoundIncompatible},
		{name: "host newer", hostRelease: "1.0.2", relation: pb.ReleaseRelation_RELEASE_RELATION_HOST_NEWER, client: "1.0.1", hostDigest: digest, configOfRoot: digest, want: clientcli.FoundIncompatible},
		{name: "host of no release", hostRelease: "dev", relation: pb.ReleaseRelation_RELEASE_RELATION_UNKNOWN, client: "1.0.1", hostDigest: digest, configOfRoot: digest, want: clientcli.FoundIncompatible},
		{name: "client of no release", hostRelease: "1.0.1", relation: pb.ReleaseRelation_RELEASE_RELATION_UNKNOWN, client: "dev", hostDigest: digest, configOfRoot: digest, want: clientcli.FoundIncompatible},
		{name: "two builds of no release", hostRelease: "dev", relation: pb.ReleaseRelation_RELEASE_RELATION_UNKNOWN, client: "dev", hostDigest: digest, configOfRoot: digest, want: clientcli.FoundCompatible},
		{name: "same release", hostRelease: "1.0.1", relation: pb.ReleaseRelation_RELEASE_RELATION_SAME, client: "1.0.1", hostDigest: digest, configOfRoot: digest, want: clientcli.FoundCompatible},
		{name: "the root gained a configuration", hostRelease: "1.0.1", relation: pb.ReleaseRelation_RELEASE_RELATION_SAME, client: "1.0.1", hostDigest: "", configOfRoot: digest, want: clientcli.FoundConfigMismatch},
		{name: "the root lost its configuration", hostRelease: "1.0.1", relation: pb.ReleaseRelation_RELEASE_RELATION_SAME, client: "1.0.1", hostDigest: digest, configOfRoot: "", want: clientcli.FoundConfigMismatch},
		{name: "the root changed its configuration", hostRelease: "1.0.1", relation: pb.ReleaseRelation_RELEASE_RELATION_SAME, client: "1.0.1", hostDigest: "older", configOfRoot: digest, want: clientcli.FoundConfigMismatch},
		{name: "another release on another configuration", hostRelease: "1.0.0", relation: pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER, client: "1.0.1", hostDigest: "older", configOfRoot: digest, want: clientcli.FoundIncompatible},
	} {
		id := &pb.IdentifyResponse{Release: c.hostRelease, Relation: c.relation, ConfigDigest: c.hostDigest}
		if got := clientcli.Classify(id, c.client, c.configOfRoot); got != c.want {
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

// A launch on a host of another release with live runs is refused with exit 1, says what to do, and
// asks the host for nothing: it is not stopped, in either direction of the mismatch. The same for a
// host on another configuration.
func TestALaunchIsRefusedOnAMismatchedHostWithLiveRunsAndNoRunIsTouched(t *testing.T) {
	for name, make := range map[string]func() (*fakeHost, *fakeConfigs){
		"host older": func() (*fakeHost, *fakeConfigs) {
			id := identity(2)
			id.Release, id.Relation = "0.9.0", pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER
			return &fakeHost{running: true, identity: id}, &fakeConfigs{}
		},
		"host newer": func() (*fakeHost, *fakeConfigs) {
			id := identity(2)
			id.Release, id.Relation = "1.1.0", pb.ReleaseRelation_RELEASE_RELATION_HOST_NEWER
			return &fakeHost{running: true, identity: id}, &fakeConfigs{}
		},
		"another configuration": func() (*fakeHost, *fakeConfigs) {
			return &fakeHost{running: true, identity: identity(2)}, &fakeConfigs{config: &pb.HostConfig{JobTool: "/x/vibesys-cli"}}
		},
	} {
		t.Run(name, func(t *testing.T) {
			h, configs := make()
			env := hostWorld(h, nil)
			env.Configs = configs
			env.FS = fstest.MapFS{"work/repo/.git": {}}
			env.Dir = "/work/repo"
			env.Canonical = func(path string) string { return path }
			env.IDs = &counterIDs{}
			code, _, errb := runHost(env, "run", "start", "--task", "t", "--detach")
			if code != clientcli.ExitFailed || !strings.Contains(errb, "never replaced under live runs") {
				t.Fatalf("exit %d: %s", code, errb)
			}
			if len(h.stops) != 0 || h.starts != 0 || !h.isRunning() {
				t.Fatalf("the host was touched: stops %v, starts %d, running %v", h.stops, h.starts, h.isRunning())
			}
		})
	}
}

// An idle host that is the wrong one is asked to stop only if idle, is replaced, and the client says
// so on stderr, in both directions and for a configuration.
func TestAnIdleMismatchedHostIsReplacedAndNeverStoppedUnconditionally(t *testing.T) {
	for name, make := range map[string]func() (*fakeHost, *fakeConfigs){
		"host older": func() (*fakeHost, *fakeConfigs) {
			id := identity(0)
			id.Release, id.Relation = "0.9.0", pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER
			return &fakeHost{running: true, identity: id}, &fakeConfigs{}
		},
		"host newer": func() (*fakeHost, *fakeConfigs) {
			id := identity(0)
			id.Release, id.Relation = "1.1.0", pb.ReleaseRelation_RELEASE_RELATION_HOST_NEWER
			return &fakeHost{running: true, identity: id}, &fakeConfigs{}
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
			_, _, errb := runHost(env, "run", "list")
			if len(h.stops) != 1 || !h.stops[0] {
				t.Fatalf("stops %v, want one, only if idle", h.stops)
			}
			if h.starts != 1 || !strings.Contains(errb, "replaced the host") {
				t.Fatalf("starts %d: %s", h.starts, errb)
			}
		})
	}
}

// A read on a mismatched host with live runs goes on, with a note, and never stops it.
func TestAReadOnAMismatchedHostWithLiveRunsGoesOnAndSaysSo(t *testing.T) {
	id := identity(1)
	id.Release, id.Relation = "0.9.0", pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER
	h := &fakeHost{running: true, identity: id}
	_, _, errb := runHost(hostWorld(h, nil), "run", "list")
	if len(h.stops) != 0 || h.starts != 0 || !strings.Contains(errb, "so it stays until they end") {
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
