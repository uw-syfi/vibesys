package main_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	"github.com/uw-syfi/vibesys/orchestrator/gatewayroot"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
	"github.com/uw-syfi/vibesys/orchestrator/hostlock"
	"github.com/uw-syfi/vibesys/orchestrator/release/releasetest"
)

// programs is a real vibesys-client and vibesys-host, side by side, as in a developer's build.
type programs struct{ dir, client, host string }

// built is the one build of the two programs in this test binary: linking them takes seconds, and no
// test changes them. A test that means to works on its own copy, from ownCopy.
var built struct {
	once sync.Once
	p    programs
	err  error
}

func buildBoth(t *testing.T) programs {
	t.Helper()
	built.once.Do(func() { built.p, built.err = buildPrograms() })
	if built.err != nil {
		t.Fatal(built.err)
	}
	return built.p
}

func buildPrograms() (programs, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	dir, err := os.MkdirTemp("", "programs")
	if err != nil {
		return programs{}, err
	}
	p := programs{dir: dir, client: filepath.Join(dir, "vibesys-client"), host: filepath.Join(dir, "vibesys-host")}
	if output, err := exec.CommandContext(ctx, "go", "build", "-o", p.client, ".").CombinedOutput(); err != nil {
		return p, fmt.Errorf("build the client: %v\n%s", err, output)
	}
	if output, err := exec.CommandContext(ctx, "go", "build", "-o", p.host, "../vibesys-host").CombinedOutput(); err != nil {
		return p, fmt.Errorf("build the host: %v\n%s", err, output)
	}
	return p, nil
}

// TestMain removes the one build.
func TestMain(m *testing.M) {
	code := m.Run()
	if built.p.dir != "" {
		os.RemoveAll(built.p.dir)
	}
	os.Exit(code)
}

// ownCopy is the two programs in a directory of this test's alone.
func (p programs) ownCopy(t *testing.T) programs {
	t.Helper()
	dir := t.TempDir()
	own := programs{dir: dir, client: filepath.Join(dir, "vibesys-client"), host: filepath.Join(dir, "vibesys-host")}
	for from, to := range map[string]string{p.client: own.client, p.host: own.host} {
		data, err := os.ReadFile(from)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(to, data, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return own
}

// shortDir is a directory whose own path leaves a socket room to fit.
func shortDir(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "c")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	real, err := filepath.EvalSymlinks(dir)
	if err != nil {
		t.Fatal(err)
	}
	return real
}

// clientIn runs the client with exactly env, plus the PATH that finds its host.
func (p programs) clientIn(t *testing.T, env map[string]string, args ...string) (code int, stdout, stderr string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, p.client, args...)
	command.Env = []string{"PATH=" + p.dir + ":/usr/bin:/bin"}
	for name, value := range env {
		command.Env = append(command.Env, name+"="+value)
	}
	var out, errb bytes.Buffer
	command.Stdout, command.Stderr = &out, &errb
	var exit *exec.ExitError
	switch err := command.Run(); {
	case err == nil:
	case errors.As(err, &exit):
		code = exit.ExitCode()
	default:
		t.Fatal(err)
	}
	return code, out.String(), errb.String()
}

// stopHost ends the host of a root when a test is over, whatever it did.
func (p programs) stopHost(t *testing.T, root string) {
	t.Helper()
	t.Cleanup(func() {
		p.clientIn(t, map[string]string{"HOME": root}, "--root", root, "host", "stop", "--force")
	})
}

// noHostLeft fails unless nothing holds the root and nothing listens.
func noHostLeft(t *testing.T, root string) {
	t.Helper()
	if _, held, err := lifecycle.Holder(root); err != nil || held {
		t.Errorf("a process holds %s: %v, %v", root, held, err)
	}
	if _, err := os.Stat(lifecycle.SocketPath(root)); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("a socket is left at %s: %v", lifecycle.SocketPath(root), err)
	}
}

// With VIBESYS_HOME set and no flag the client's host is there, not under ~/.vibesys. A client left on
// the old root is how a refused start left a stray host in the wrong place.
func TestVIBESYSHOMEIsTheClientsRoot(t *testing.T) {
	p := buildBoth(t)
	home, chosen := shortDir(t), shortDir(t)
	p.stopHost(t, chosen)
	env := map[string]string{"HOME": home, "VIBESYS_HOME": chosen}
	if code, _, stderr := p.clientIn(t, env, "run", "list"); code != 0 {
		t.Fatalf("run list: exit %d: %s", code, stderr)
	}
	if _, err := os.Stat(filepath.Join(chosen, "host.sock")); err != nil {
		t.Errorf("no host at VIBESYS_HOME: %v", err)
	}
	if _, err := os.Stat(filepath.Join(home, ".vibesys")); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("the default root was touched: %v", err)
	}
	code, stdout, stderr := p.clientIn(t, env, "host", "status")
	if code != 0 || !strings.Contains(stdout, "orchestrator: serving") {
		t.Errorf("host status: exit %d: %s%s", code, stdout, stderr)
	}
	if _, err := os.Stat(filepath.Join(chosen, "state", "agent.sock")); err != nil {
		t.Errorf("the state did not follow the root: %v", err)
	}
}

// installedClient lays a release in HOME/releases/1.0.0 with the real client in it, which is how the
// installer leaves it, and returns the program and HOME.
func (p programs) installedClient(t *testing.T) (programs, string) {
	t.Helper()
	home := shortDir(t)
	release := filepath.Join(home, "releases", "1.0.0")
	releasetest.Write(t, release)
	data, err := os.ReadFile(p.client)
	if err != nil {
		t.Fatal(err)
	}
	installed := filepath.Join(release, "bin", "vibesys-client")
	if err := os.Remove(installed); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(installed, data, 0o755); err != nil {
		t.Fatal(err)
	}
	return programs{dir: filepath.Dir(installed), client: installed, host: filepath.Join(filepath.Dir(installed), "vibesys-host")}, home
}

// An installed client's root is where it is installed when VIBESYS_HOME is not set, so installing with
// the setting and later running without it still agree. When the setting names somewhere else it is
// an error printing the rule, never one place or the other; the flag decides.
func TestAnInstalledClientAndVIBESYSHOMEAgreeOrItSaysTheRule(t *testing.T) {
	installed, home := buildBoth(t).installedClient(t)
	user := shortDir(t)

	tool := installed.client
	if code, _, stderr := installed.clientIn(t, map[string]string{"HOME": user}, "host", "configure", "--job-tool", tool); code != 0 {
		t.Fatalf("no setting: exit %d: %s", code, stderr)
	}
	if config, err := hostconfig.Load(home); err != nil || config.GetJobTool() != tool {
		t.Fatalf("the install's root has %v, %v", config, err)
	}
	if _, err := os.Stat(filepath.Join(user, ".vibesys")); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("the default root was touched: %v", err)
	}
	if code, _, stderr := installed.clientIn(t, map[string]string{"HOME": user, "VIBESYS_HOME": home}, "host", "configure"); code != 0 {
		t.Errorf("the setting agrees: exit %d: %s", code, stderr)
	}

	elsewhere := shortDir(t)
	code, _, stderr := installed.clientIn(t, map[string]string{"HOME": user, "VIBESYS_HOME": elsewhere}, "host", "status")
	if code != 2 || !strings.Contains(stderr, gatewayroot.Rule) || !strings.Contains(stderr, elsewhere) || !strings.Contains(stderr, home) {
		t.Errorf("they disagree: exit %d: %s", code, stderr)
	}
	if code, _, stderr := installed.clientIn(t, map[string]string{"HOME": user, "VIBESYS_HOME": elsewhere}, "--root", elsewhere, "host", "status"); code != 4 {
		t.Errorf("the flag decides: exit %d: %s", code, stderr)
	}
}

// A refused start exits 1 with only why this start was refused, not the lines an older host left in
// the log, and leaves no host. On a state directory the host cannot use.
func TestARefusedStartLeavesNoHostAndShowsOnlyItsOwnFailure(t *testing.T) {
	p := buildBoth(t)
	root := shortDir(t)
	blocker := filepath.Join(root, "a-file")
	if err := os.WriteFile(blocker, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := hostconfig.Save(root, &pb.HostConfig{StateDirectory: filepath.Join(blocker, "state")}); err != nil {
		t.Fatal(err)
	}
	const old = "2026-11-03 an older host said this"
	if err := os.WriteFile(lifecycle.LogPath(root), []byte(old+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	env := map[string]string{"HOME": shortDir(t)}
	code, stdout, stderr := p.clientIn(t, env, "--root", root, "run", "list")
	if code != 1 || !strings.Contains(stderr, "no host can start") || !strings.Contains(stderr, "a-file") {
		t.Errorf("exit %d, stdout %q, stderr %q", code, stdout, stderr)
	}
	if strings.Contains(stderr, old) {
		t.Errorf("an old log was shown as the reason: %s", stderr)
	}
	noHostLeft(t, root)
}

// A configuration that does not read is refused the same way, naming the key.
func TestAConfigurationThatDoesNotReadRefusesTheStart(t *testing.T) {
	p := buildBoth(t)
	root := shortDir(t)
	if err := os.WriteFile(hostconfig.Path(root), []byte(`{"stateDirectry": "/s"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	code, _, stderr := p.clientIn(t, map[string]string{"HOME": shortDir(t)}, "--root", root, "run", "list")
	if code != 1 || !strings.Contains(stderr, "stateDirectry") {
		t.Errorf("exit %d: %s", code, stderr)
	}
	noHostLeft(t, root)
}

// A client that starts a host and a client that finds one reach the same socket, through a symlinked
// home and for a root too long for a socket address, each spelled every way.
func TestAStarterAndAFinderGetTheSameSocket(t *testing.T) {
	p := buildBoth(t)
	cases := map[string]string{
		"short": shortDir(t),
		"long":  filepath.Join(shortDir(t), strings.Repeat("r", 120)),
	}
	for name, root := range cases {
		t.Run(name, func(t *testing.T) {
			if err := os.MkdirAll(root, 0o700); err != nil {
				t.Fatal(err)
			}
			link := filepath.Join(shortDir(t), "link")
			if err := os.Symlink(root, link); err != nil {
				t.Fatal(err)
			}
			p.stopHost(t, root)
			env := map[string]string{"HOME": shortDir(t)}
			// The host's own socket and the sockets of its default state fit whatever the root:
			// nothing is configured for a long one.
			if code, _, stderr := p.clientIn(t, env, "--root", link, "run", "list"); code != 0 {
				log, _ := os.ReadFile(lifecycle.LogPath(root))
				t.Fatalf("started through the link: exit %d: %s\nhost log: %s", code, stderr, log)
			}
			for _, spelled := range []string{root, link, root + "/", link + "/."} {
				if code, stdout, stderr := p.clientIn(t, env, "--root", spelled, "host", "status"); code != 0 || !strings.Contains(stdout, "serving") {
					t.Errorf("found through %s: exit %d: %s%s", spelled, code, stdout, stderr)
				}
			}
			if code, _, stderr := p.clientIn(t, map[string]string{"HOME": shortDir(t), "VIBESYS_HOME": link}, "host", "status"); code != 0 {
				t.Errorf("found through VIBESYS_HOME: exit %d: %s", code, stderr)
			}
			if name == "long" && strings.HasPrefix(lifecycle.SocketPath(root), root) {
				t.Errorf("the socket %s is inside a root too long for it", lifecycle.SocketPath(root))
			}
		})
	}
}

// A dead host's log is nobody's business on a client's next command: output starts with the verb's.
func TestRestartingADeadHostPrintsNothingOfItsLog(t *testing.T) {
	p := buildBoth(t)
	root := shortDir(t)
	p.stopHost(t, root)
	if err := os.WriteFile(lifecycle.LogPath(root), []byte(strings.Repeat("an old line\n", 12)), 0o600); err != nil {
		t.Fatal(err)
	}
	code, stdout, stderr := p.clientIn(t, map[string]string{"HOME": shortDir(t)}, "--root", root, "run", "list")
	if code != 0 || strings.Contains(stdout+stderr, "an old line") {
		t.Errorf("exit %d, stdout %q, stderr %q", code, stdout, stderr)
	}
}

// startedAt is when the host of root says it started, which only a new host changes.
func (p programs) startedAt(t *testing.T, env map[string]string, root string) string {
	t.Helper()
	code, stdout, stderr := p.clientIn(t, env, "--root", root, "host", "status", "--json")
	if code != 0 {
		t.Fatalf("status: exit %d: %s", code, stderr)
	}
	var id struct {
		StartedAt string `json:"startedAt"`
	}
	if err := json.Unmarshal([]byte(stdout), &id); err != nil || id.StartedAt == "" {
		t.Fatalf("status: %v: %s", err, stdout)
	}
	return id.StartedAt
}

// gitRepository is a repository for a run start, which then goes as far as it can without a task.
func gitRepository(t *testing.T) string {
	t.Helper()
	repository := shortDir(t)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if output, err := exec.CommandContext(ctx, "git", "init", "-q", repository).CombinedOutput(); err != nil {
		t.Fatalf("git init: %v\n%s", err, output)
	}
	return repository
}

// A changed configuration leaves a read alone, with a note. A run start replaces the idle host, once,
// and the new one reports it.
func TestAChangedConfigurationIsTakenUpByARunStartAndNotByARead(t *testing.T) {
	p := buildBoth(t)
	root := shortDir(t)
	p.stopHost(t, root)
	env := map[string]string{"HOME": shortDir(t)}
	if code, _, stderr := p.clientIn(t, env, "--root", root, "run", "list"); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr)
	}
	first := p.startedAt(t, env, root)
	if code, _, stderr := p.clientIn(t, env, "--root", root, "host", "configure", "--tool-path", "/usr/bin:/bin"); code != 0 {
		t.Fatalf("configure: exit %d: %s", code, stderr)
	}
	code, _, stderr := p.clientIn(t, env, "--root", root, "run", "list")
	if code != 0 || strings.Contains(stderr, "replaced") || !strings.Contains(stderr, "another configuration") {
		t.Fatalf("a read: exit %d: %s", code, stderr)
	}
	if now := p.startedAt(t, env, root); now != first {
		t.Fatalf("a read replaced the host: %s, then %s", first, now)
	}
	repository := gitRepository(t)
	_, _, stderr = p.clientIn(t, env, "--root", root, "run", "start", "--repository", repository, "--task", "t", "--detach")
	if !strings.Contains(stderr, "replaced the host") || !strings.Contains(stderr, "another configuration") {
		t.Fatalf("a run start: %s", stderr)
	}
	_, _, stderr = p.clientIn(t, env, "--root", root, "run", "start", "--repository", repository, "--task", "t", "--detach")
	if strings.Contains(stderr, "replaced") {
		t.Fatalf("the second time: %s", stderr)
	}
}

// Finding: a bad host-config.json blocked every verb on a healthy host, and could not be repaired. Verbs
// go on naming the file and the key; --clear repairs it; with no host, a verb that must start one refuses.
func TestABadConfigurationLeavesAHealthyHostUsableAndCanBeRepaired(t *testing.T) {
	p := buildBoth(t)
	root := shortDir(t)
	p.stopHost(t, root)
	env := map[string]string{"HOME": shortDir(t)}
	if code, _, stderr := p.clientIn(t, env, "--root", root, "run", "list"); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr)
	}
	first := p.startedAt(t, env, root)
	if err := os.WriteFile(hostconfig.Path(root), []byte(`{"stateDirectry": "/s"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, verb := range [][]string{{"run", "list"}, {"host", "status"}} {
		code, _, stderr := p.clientIn(t, env, append([]string{"--root", root}, verb...)...)
		if code != 0 || !strings.Contains(stderr, "host-config.json") || !strings.Contains(stderr, "stateDirectry") || !strings.Contains(stderr, "--clear") {
			t.Errorf("%v: exit %d: %s", verb, code, stderr)
		}
	}
	if now := p.startedAt(t, env, root); now != first {
		t.Fatalf("the host was replaced: %s, then %s", first, now)
	}
	if code, _, stderr := p.clientIn(t, env, "--root", root, "host", "restart"); code != 1 || !strings.Contains(stderr, "stays as it is") || !strings.Contains(stderr, "stateDirectry") {
		t.Errorf("restart: exit %d: %s", code, stderr)
	}
	if now := p.startedAt(t, env, root); now != first {
		t.Fatalf("a restart that could not start one stopped the host: %s, then %s", first, now)
	}
	if code, _, stderr := p.clientIn(t, env, "--root", root, "host", "stop"); code != 0 {
		t.Fatalf("stop: exit %d: %s", code, stderr)
	}
	if code, _, stderr := p.clientIn(t, env, "--root", root, "run", "list"); code != 1 || !strings.Contains(stderr, "stateDirectry") || !strings.Contains(stderr, "--clear") {
		t.Errorf("with no host: exit %d: %s", code, stderr)
	}
	noHostLeft(t, root)
	if code, _, stderr := p.clientIn(t, env, "--root", root, "host", "configure", "--clear"); code != 0 {
		t.Fatalf("--clear: exit %d: %s", code, stderr)
	}
	if code, _, stderr := p.clientIn(t, env, "--root", root, "run", "list"); code != 0 {
		t.Errorf("repaired: exit %d: %s", code, stderr)
	}
}

// Finding: a client replaced a working host before finding it could not start one. An unservable state
// directory is refused by configure, and written by hand leaves the host up, for a read and a run start.
func TestAHostStaysWhenNoNewOneCouldStart(t *testing.T) {
	p := buildBoth(t)
	root := shortDir(t)
	p.stopHost(t, root)
	env := map[string]string{"HOME": shortDir(t)}
	if code, _, stderr := p.clientIn(t, env, "--root", root, "run", "list"); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr)
	}
	first := p.startedAt(t, env, root)
	blocker := filepath.Join(root, "a-file")
	if err := os.WriteFile(blocker, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	bad := filepath.Join(blocker, "state")
	if code, _, stderr := p.clientIn(t, env, "--root", root, "host", "configure", "--state-directory", bad); code != 1 || !strings.Contains(stderr, "a-file") {
		t.Errorf("configure: exit %d: %s", code, stderr)
	}
	if _, err := os.Stat(hostconfig.Path(root)); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("a refused configuration was written: %v", err)
	}
	if err := hostconfig.Save(root, &pb.HostConfig{StateDirectory: bad}); err != nil {
		t.Fatal(err)
	}
	if code, _, stderr := p.clientIn(t, env, "--root", root, "run", "list"); code != 0 {
		t.Errorf("a read: exit %d: %s", code, stderr)
	}
	repository := gitRepository(t)
	code, _, stderr := p.clientIn(t, env, "--root", root, "run", "start", "--repository", repository, "--task", "t", "--detach")
	if code != 1 || !strings.Contains(stderr, "stays as it is") || !strings.Contains(stderr, "a-file") {
		t.Errorf("a run start: exit %d: %s", code, stderr)
	}
	if now := p.startedAt(t, env, root); now != first {
		t.Fatalf("the host was replaced: %s, then %s", first, now)
	}
}

// A client that cannot find a host program still reads the host that is there.
func TestAClientWithNoHostProgramReadsTheHostThatIsThere(t *testing.T) {
	p := buildBoth(t)
	root := shortDir(t)
	p.stopHost(t, root)
	env := map[string]string{"HOME": shortDir(t)}
	if code, _, stderr := p.clientIn(t, env, "--root", root, "run", "list"); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr)
	}
	alone := filepath.Join(shortDir(t), "vibesys-client")
	data, err := os.ReadFile(p.client)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(alone, data, 0o755); err != nil {
		t.Fatal(err)
	}
	apart := programs{dir: filepath.Dir(alone), client: alone}
	if code, _, stderr := apart.clientIn(t, env, "--root", root, "run", "list"); code != 0 {
		t.Errorf("exit %d: %s", code, stderr)
	}
}

// Finding: two builds of no release always compared equal, so a rebuilt client kept an old host. A read
// says it found another build and leaves it; a run start replaces it, and then has the same one.
func TestARebuiltHostProgramIsToldApart(t *testing.T) {
	p := buildBoth(t).ownCopy(t)
	root := shortDir(t)
	p.stopHost(t, root)
	env := map[string]string{"HOME": shortDir(t)}
	if code, _, stderr := p.clientIn(t, env, "--root", root, "run", "list"); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr)
	}
	if _, _, stderr := p.clientIn(t, env, "--root", root, "run", "list"); strings.Contains(stderr, "another build") {
		t.Fatalf("the same build was told apart: %s", stderr)
	}
	data, err := os.ReadFile(p.host)
	if err != nil {
		t.Fatal(err)
	}
	rebuilt := p.host + ".new"
	if err := os.WriteFile(rebuilt, append(data, 0), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(rebuilt, p.host); err != nil {
		t.Fatal(err)
	}
	first := p.startedAt(t, env, root)
	code, _, stderr := p.clientIn(t, env, "--root", root, "run", "list")
	if code != 0 || !strings.Contains(stderr, "another build") {
		t.Fatalf("a read: exit %d: %s", code, stderr)
	}
	if now := p.startedAt(t, env, root); now != first {
		t.Fatalf("a read replaced it")
	}
	repository := gitRepository(t)
	_, _, stderr = p.clientIn(t, env, "--root", root, "run", "start", "--repository", repository, "--task", "t", "--detach")
	if !strings.Contains(stderr, "replaced the host") || !strings.Contains(stderr, "another build") {
		t.Fatalf("a run start: %s", stderr)
	}
	if _, _, stderr := p.clientIn(t, env, "--root", root, "run", "list"); strings.Contains(stderr, "another build") {
		t.Fatalf("the new host is not the program it started: %s", stderr)
	}
}

// Finding: a client replaced a host while another client's connection was open. It counts them, a run
// start refuses naming host restart, and once they have closed the host is idle again.
func TestAConnectedClientKeepsAHostFromBeingReplaced(t *testing.T) {
	p := buildBoth(t)
	root := shortDir(t)
	p.stopHost(t, root)
	env := map[string]string{"HOME": shortDir(t)}
	if code, _, stderr := p.clientIn(t, env, "--root", root, "run", "list"); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr)
	}
	other, err := net.Dial("unix", lifecycle.SocketPath(root))
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	code, stdout, stderr := p.clientIn(t, env, "--root", root, "host", "status")
	if code != 0 || !strings.Contains(stdout, "other clients connected: 1") {
		t.Fatalf("status: exit %d: %s%s", code, stdout, stderr)
	}
	if code, _, stderr := p.clientIn(t, env, "--root", root, "host", "configure", "--tool-path", "/usr/bin:/bin"); code != 0 {
		t.Fatalf("configure: exit %d: %s", code, stderr)
	}
	repository := gitRepository(t)
	code, _, stderr = p.clientIn(t, env, "--root", root, "run", "start", "--repository", repository, "--task", "t", "--detach")
	if code != 1 || !strings.Contains(stderr, "1 other client") || !strings.Contains(stderr, "host restart") {
		t.Fatalf("a run start: exit %d: %s", code, stderr)
	}
	other.Close()
	_, _, stderr = p.clientIn(t, env, "--root", root, "run", "start", "--repository", repository, "--task", "t", "--detach")
	if !strings.Contains(stderr, "replaced the host") {
		t.Fatalf("with the other client gone: %s", stderr)
	}
}

// Finding: from another machine on a shared home the socket refuses, the lock is held and the number in
// it is some other program's. Here the number is this test's own: had anything signalled it, the test
// would end there. Every verb answers at once naming the machine.
func TestAHostOnAnotherMachineIsNamedAndNeverSignalled(t *testing.T) {
	p := buildBoth(t)
	root := shortDir(t)
	held, err := lifecycle.Hold(root)
	if err != nil {
		t.Fatal(err)
	}
	defer held.Close()
	record, err := hostlock.Marshal(os.Getpid(), "1.0.0", root, "host-b")
	if err != nil {
		t.Fatal(err)
	}
	if err := held.Record(record); err != nil {
		t.Fatal(err)
	}
	repository := gitRepository(t)
	env := map[string]string{"HOME": shortDir(t)}
	for _, verb := range [][]string{
		{"run", "list"}, {"run", "start", "--repository", repository, "--task", "t", "--detach"},
		{"host", "status"}, {"host", "stop"}, {"host", "stop", "--force"}, {"host", "stop", "--if-no-live-runs"}, {"host", "restart"},
	} {
		code, _, stderr := p.clientIn(t, env, append([]string{"--root", root}, verb...)...)
		if code != 4 || !strings.Contains(stderr, "host-b") {
			t.Errorf("%v: exit %d: %s", verb, code, stderr)
		}
	}
}

// A retired setting is named when a start ignores it, so that it does not stop working silently.
func TestARetiredSettingIsNamedWhenAStartIgnoresIt(t *testing.T) {
	p := buildBoth(t)
	root := shortDir(t)
	p.stopHost(t, root)
	for _, setting := range hostconfig.Retired {
		t.Run(setting.Name, func(t *testing.T) {
			p.clientIn(t, map[string]string{"HOME": root}, "--root", root, "host", "stop")
			code, _, stderr := p.clientIn(t, map[string]string{"HOME": shortDir(t), setting.Name: "/nonsense"}, "--root", root, "run", "list")
			if code != 0 || !strings.Contains(stderr, setting.Name) || !strings.Contains(stderr, setting.Flag) {
				t.Errorf("exit %d: %s", code, stderr)
			}
		})
	}
}

// A host the desktop app provisioned is in no release, so it still refuses every launch, as it did.
// Its bridge starts it; the client finds it, as a build of no release of its own, and says why.
func TestADesktopProvisionedHostStillRefusesLaunches(t *testing.T) {
	p := buildBoth(t)
	root := shortDir(t)
	p.stopHost(t, root)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	bridge := exec.CommandContext(ctx, p.host, "connect", "--root", root)
	bridge.Env = []string{"HOME=" + shortDir(t), "PATH=/usr/bin:/bin"}
	bridge.Stdin = bytes.NewReader(nil)
	if output, err := bridge.CombinedOutput(); err != nil {
		t.Fatalf("connect: %v\n%s", err, output)
	}
	env := map[string]string{"HOME": shortDir(t)}
	code, stdout, stderr := p.clientIn(t, env, "--root", root, "host", "status")
	if code != 0 || !strings.Contains(stdout, "session holder: cannot be used") {
		t.Fatalf("status: exit %d: %s%s", code, stdout, stderr)
	}
	repository := shortDir(t)
	if output, err := exec.CommandContext(ctx, "git", "init", "-q", repository).CombinedOutput(); err != nil {
		t.Fatalf("git init: %v\n%s", err, output)
	}
	code, _, stderr = p.clientIn(t, env, "--root", root, "run", "start", "--repository", repository, "--task", "t", "--detach")
	if code != 1 || !strings.Contains(stderr, "no agent session can start") {
		t.Fatalf("a launch: exit %d: %s", code, stderr)
	}
}

// Finding: the host's cleared environment dropped what the job tool reads, so its daemon ignored the
// operator's settings. What the host starts is what shows it: its session holder here writes down what
// it was given, and the run fails as it would for any holder that does not answer. A retired setting in
// the same environment is not there.
func TestAHostStartedFromAHostileEnvironmentKeepsTheJobToolSettings(t *testing.T) {
	p := buildBoth(t)
	root := shortDir(t)
	p.stopHost(t, root)
	seen := filepath.Join(root, "seen")
	holder := filepath.Join(root, "holder.sh")
	if err := os.WriteFile(holder, []byte("#!/bin/sh\nenv > "+seen+"\nexit 1\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, ".claude-plugin"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, ".claude-plugin", "marketplace.json"), []byte(`{"plugins":[{"name":"vibesys"}]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	// A start asks the job tool which protocol it speaks, then for its daemon; this one speaks a new
	// one and answers with a daemon that nobody needs to reach.
	tool := filepath.Join(root, "job-tool.sh")
	answer := `{"socket":"` + root + `/rt/daemon.sock","runtimeDirectory":"` + root + `/rt"}`
	script := "#!/bin/sh\nif [ \"$1\" = --version ]; then echo '{\"protocol_version\": 99}'; exit 0; fi\necho '" + answer + "'\n"
	if err := os.WriteFile(tool, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := hostconfig.Save(root, &pb.HostConfig{SessionHolderCommand: holder, JobTool: tool, SkillsDirectory: root}); err != nil {
		t.Fatal(err)
	}
	repository := gitRepository(t)
	if err := os.MkdirAll(filepath.Join(repository, ".vibesys", "tasks", "t"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repository, ".vibesys", "tasks", "t", "vibesys.input.toml"), []byte("version = \"0.1\"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	for _, args := range [][]string{{"add", "-A"}, {"-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "t"}} {
		git := exec.CommandContext(ctx, "git", args...)
		git.Dir = repository
		if output, err := git.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, output)
		}
	}
	env := map[string]string{
		"HOME": shortDir(t), "VIBESYS_CLI_CONFIG": "/hostile/cli.toml", "XDG_CONFIG_HOME": "/hostile/config", "XDG_CACHE_HOME": "/hostile/cache",
		"VIBESYS_ORCHESTRATOR_CLI": "/hostile/retired", "ANYTHING_ELSE": "/hostile/else",
	}
	code, _, stderr := p.clientIn(t, env, "--root", root, "run", "start", "--repository", repository, "--task", "t", "--detach")
	got, err := os.ReadFile(seen)
	if err != nil {
		t.Fatalf("the session holder never ran (exit %d: %s): %v", code, stderr, err)
	}
	for _, want := range []string{"VIBESYS_CLI_CONFIG=/hostile/cli.toml", "XDG_CONFIG_HOME=/hostile/config", "XDG_CACHE_HOME=/hostile/cache"} {
		if !strings.Contains(string(got), want) {
			t.Errorf("the host's environment lacks %s:\n%s", want, got)
		}
	}
	for _, refused := range []string{"VIBESYS_ORCHESTRATOR_CLI", "ANYTHING_ELSE"} {
		if strings.Contains(string(got), refused) {
			t.Errorf("%s came through:\n%s", refused, got)
		}
	}
}
