package main_test

import (
	"bytes"
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	"github.com/uw-syfi/vibesys/orchestrator/gatewayroot"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
	"github.com/uw-syfi/vibesys/orchestrator/release/releasetest"
)

// programs is a real vibesys-client and vibesys-host, side by side, as in a developer's build.
type programs struct{ dir, client, host string }

func buildBoth(t *testing.T) programs {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	dir := t.TempDir()
	p := programs{dir: dir, client: filepath.Join(dir, "vibesys-client"), host: filepath.Join(dir, "vibesys-host")}
	if output, err := exec.CommandContext(ctx, "go", "build", "-o", p.client, ".").CombinedOutput(); err != nil {
		t.Fatalf("build the client: %v\n%s", err, output)
	}
	if output, err := exec.CommandContext(ctx, "go", "build", "-o", p.host, "../vibesys-host").CombinedOutput(); err != nil {
		t.Fatalf("build the host: %v\n%s", err, output)
	}
	return p
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

	if code, _, stderr := installed.clientIn(t, map[string]string{"HOME": user}, "host", "configure", "--job-tool", "/x/tool"); code != 0 {
		t.Fatalf("no setting: exit %d: %s", code, stderr)
	}
	if config, err := hostconfig.Load(home); err != nil || config.GetJobTool() != "/x/tool" {
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
	if code != 1 || !strings.Contains(stderr, "the host refused to start") || !strings.Contains(stderr, "a-file") {
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
			if name == "long" {
				// The host's own socket fits whatever the root, and the sockets of its state do not
				// follow it there: a state directory this long is refused saying how to choose one.
				code, _, stderr := p.clientIn(t, env, "--root", link, "run", "list")
				if code != 1 || !strings.Contains(stderr, "--state-directory") {
					t.Fatalf("a long default state: exit %d: %s", code, stderr)
				}
				noHostLeft(t, root)
				if code, _, stderr := p.clientIn(t, env, "--root", link, "host", "configure", "--state-directory", shortDir(t)); code != 0 {
					t.Fatalf("configure: exit %d: %s", code, stderr)
				}
			}
			if code, _, stderr := p.clientIn(t, env, "--root", link, "run", "list"); code != 0 {
				t.Fatalf("started through the link: exit %d: %s", code, stderr)
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

// Changing the configuration with an idle host replaces the host, once, and the new one reports it.
func TestAChangedConfigurationReplacesAnIdleHost(t *testing.T) {
	p := buildBoth(t)
	root := shortDir(t)
	p.stopHost(t, root)
	env := map[string]string{"HOME": shortDir(t)}
	if code, _, stderr := p.clientIn(t, env, "--root", root, "run", "list"); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr)
	}
	if code, _, stderr := p.clientIn(t, env, "--root", root, "host", "configure", "--tool-path", "/usr/bin:/bin"); code != 0 {
		t.Fatalf("configure: exit %d: %s", code, stderr)
	}
	code, _, stderr := p.clientIn(t, env, "--root", root, "run", "list")
	if code != 0 || !strings.Contains(stderr, "replaced the host") || !strings.Contains(stderr, "another configuration") {
		t.Fatalf("exit %d: %s", code, stderr)
	}
	code, _, stderr = p.clientIn(t, env, "--root", root, "run", "list")
	if code != 0 || strings.Contains(stderr, "replaced") {
		t.Fatalf("the second time: exit %d: %s", code, stderr)
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
