package devlaunch

import (
	"bytes"
	"errors"
	"fmt"
	"path"
	"sort"
	"strings"
	"testing"
	"time"
)

const testConfig = `
[local]
client_directory = "clients"
python_sync_command = ["uv", "sync"]
install_command = ["pnpm", "install"]
install_inputs = ["clients/pnpm-lock.yaml", "clients/apps/*/package.json"]
install_stamp = "build/dev/install.stamp"
build_command = ["pnpm", "build"]
app_directory = "clients/apps/desktop-main"
app_command = ["pnpm", "exec", "electron", "."]
app_env = { USER_DIR = "{profile}" }
profile_directory = "build/dev/profile"

[machine]
app_hosts_files = ["~/app/hosts.json"]
ship_ref = "refs/dev/head"
sync_command = ["uv", "sync"]
helper_command = ["uv", "run", "helper"]
path_directories = ["$HOME/.local/bin"]
hello_seconds = 10
hello_poll_seconds = 3
`

// fakeClock advances only when slept on, so waits cost no real time.
type fakeClock struct{ now time.Time }

func (c *fakeClock) Now() time.Time        { return c.now }
func (c *fakeClock) Sleep(d time.Duration) { c.now = c.now.Add(d) }

type fakeFiles struct{ files map[string]string }

func (f *fakeFiles) Glob(pattern string) ([]string, error) {
	var out []string
	for name := range f.files {
		if ok, _ := path.Match(pattern, name); ok {
			out = append(out, name)
		}
	}
	sort.Strings(out)
	return out, nil
}
func (f *fakeFiles) Read(p string) ([]byte, error) {
	if v, ok := f.files[p]; ok {
		return []byte(v), nil
	}
	return nil, fmt.Errorf("%s: not found", p)
}
func (f *fakeFiles) ReadHome(p string) ([]byte, error) { return f.Read("home:" + p) }
func (f *fakeFiles) Exists(p string) bool {
	_, ok := f.files[p]
	return ok
}
func (f *fakeFiles) Write(p string, d []byte) error { f.files[p] = string(d); return nil }

// fakeRunner records each command line and answers by the first rule whose text the line contains.
type fakeRunner struct {
	clock *fakeClock
	log   []string
	cmds  []Cmd
	rules []rule
}
type rule struct {
	contains string
	out      string
	err      error
	cost     time.Duration
}

func (r *fakeRunner) Run(c Cmd) ([]byte, error) {
	line := strings.Join(c.Argv, " ")
	r.log, r.cmds = append(r.log, line), append(r.cmds, c)
	for _, ru := range r.rules {
		if strings.Contains(line, ru.contains) {
			r.clock.now = r.clock.now.Add(ru.cost)
			return []byte(ru.out), ru.err
		}
	}
	return nil, nil
}

func (r *fakeRunner) ran(sub string) bool {
	for _, l := range r.log {
		if strings.Contains(l, sub) {
			return true
		}
	}
	return false
}

type harness struct {
	env    Env
	runner *fakeRunner
	files  *fakeFiles
	out    *bytes.Buffer
	cfg    Config
}

func newHarness(t *testing.T, files map[string]string, rules ...rule) *harness {
	t.Helper()
	cfg, err := ParseConfig("test", testConfig)
	if err != nil {
		t.Fatal(err)
	}
	clock := &fakeClock{now: time.Unix(1000, 0)}
	h := &harness{runner: &fakeRunner{clock: clock, rules: rules}, files: &fakeFiles{files: files}, out: &bytes.Buffer{}, cfg: cfg}
	h.env = Env{Root: "/repo", Runner: h.runner, Clock: clock, Files: h.files, Out: h.out}
	return h
}

func baseFiles() map[string]string {
	saved, _ := SeedHosts("host-a", "/srv/v")
	return map[string]string{
		"home:~/app/hosts.json":                  string(saved),
		"clients/pnpm-lock.yaml":                 "lock",
		"clients/apps/desktop-main/package.json": "{}",
		"clients/node_modules":                   "",
	}
}

const helloAt = `{"type":"hello","request_id":"dev-1","protocol_version":1,"features":[],"vibesys_version":"0.1.1","checkout":{"root":"/srv/v","commit":"%s","dirty":false}}` + "\n"

func TestLocalColdThenWarm(t *testing.T) {
	files := baseFiles()
	delete(files, "clients/node_modules")
	h := newHarness(t, files)
	if err := Run(h.env, h.cfg, Options{}); err != nil {
		t.Fatal(err)
	}
	if !h.runner.ran("pnpm install") || h.files.files["build/dev/install.stamp"] == "" {
		t.Fatalf("cold run must install and record a stamp: %v", h.runner.log)
	}
	h.files.files["clients/node_modules"] = ""
	h.runner.log = nil
	if err := Run(h.env, h.cfg, Options{}); err != nil {
		t.Fatal(err)
	}
	if h.runner.ran("pnpm install") || !h.runner.ran("pnpm build") {
		t.Fatalf("warm run must build without installing: %v", h.runner.log)
	}
	app := h.runner.cmds[len(h.runner.cmds)-1]
	if !app.Passthrough || len(app.Env) != 1 || app.Env[0] != "USER_DIR=/repo/build/dev/profile" {
		t.Fatalf("the app must run on the checkout's own profile, got %+v", app)
	}
	h.files.files["clients/pnpm-lock.yaml"] = "new lock"
	h.runner.log = nil
	if err := Run(h.env, h.cfg, Options{}); err != nil || !h.runner.ran("pnpm install") {
		t.Fatalf("a changed lockfile must install again: %v %v", h.runner.log, err)
	}
}

func TestFailedInstallLeavesNoStamp(t *testing.T) {
	files := baseFiles()
	delete(files, "clients/node_modules")
	h := newHarness(t, files, rule{contains: "pnpm install", err: errors.New("boom")})
	err := Run(h.env, h.cfg, Options{})
	if err == nil || !strings.Contains(err.Error(), "install client packages") || !strings.Contains(err.Error(), "boom") {
		t.Fatalf("got %v", err)
	}
	if _, ok := h.files.files["build/dev/install.stamp"]; ok || h.runner.ran("pnpm build") {
		t.Fatal("a failed install must record nothing and stop the run")
	}
}

// machineRules answers the machine probe first (its script mentions git commands too), then the
// local git queries, then extra rules.
func machineRules(branchHead, dirty string, extra ...rule) []rule {
	return append([]rule{
		{contains: "vibesys-dev-probe", out: "git\n/srv/v\n" + branchHead + "\nmain\n" + dirty + "\n"},
		{contains: "rev-parse HEAD", out: "newhead\n"},
		{contains: "status --porcelain", out: ""},
	}, extra...)
}

func TestMachineShipsSyncsAndWaitsForHello(t *testing.T) {
	h := newHarness(t, baseFiles(), machineRules("oldhead", "0",
		rule{contains: "'uv' 'run' 'helper'", out: fmt.Sprintf(helloAt, "newhead")},
		rule{contains: "'checkout'", cost: 2 * time.Second},
	)...)
	if err := Run(h.env, h.cfg, Options{Machine: "host-a"}); err != nil {
		t.Fatal(err)
	}
	order := []string{"push --force --quiet ssh://host-a/srv/v HEAD:refs/dev/head", "'--detach'", "'uv' 'sync'", "'uv' 'run' 'helper'"}
	at := 0
	for _, want := range order {
		found := false
		for ; at < len(h.runner.log); at++ {
			if strings.Contains(h.runner.log[at], want) {
				found = true
				break
			}
		}
		if !found {
			t.Fatalf("missing %q in order, log:\n%s", want, strings.Join(h.runner.log, "\n"))
		}
	}
	if !strings.Contains(h.out.String(), "done in 2s") || !strings.Contains(h.files.files["build/dev/profile/hosts.json"], `"ssh:host-a"`) {
		t.Fatalf("durations must print and the profile must be seeded:\n%s", h.out)
	}
}

func TestMachineAlreadyAtHeadShipsNothing(t *testing.T) {
	h := newHarness(t, baseFiles(), machineRules("newhead", "0", rule{contains: "'uv' 'run' 'helper'", out: fmt.Sprintf(helloAt, "newhead")})...)
	if err := Run(h.env, h.cfg, Options{Machine: "host-a"}); err != nil {
		t.Fatal(err)
	}
	if h.runner.ran("push") || h.runner.ran("--detach") {
		t.Fatalf("nothing to ship: %v", h.runner.log)
	}
}

func TestMachineRefusalsRunNothingMutating(t *testing.T) {
	for name, rules := range map[string][]rule{
		"remote dirty": machineRules("oldhead", "2"),
		"local dirty":  localDirty(),
	} {
		t.Run(name, func(t *testing.T) {
			h := newHarness(t, baseFiles(), rules...)
			err := Run(h.env, h.cfg, Options{Machine: "host-a"})
			var r Refusal
			if !errors.As(err, &r) {
				t.Fatalf("want a refusal, got %v", err)
			}
			for _, bad := range []string{"pnpm", "push", "uv", "electron"} {
				if h.runner.ran(bad) {
					t.Fatalf("%q ran before the refusal: %v", bad, h.runner.log)
				}
			}
		})
	}
}

func TestHelloTimeoutNamesStepAndWhatWasSeen(t *testing.T) {
	h := newHarness(t, baseFiles(), machineRules("oldhead", "0",
		rule{contains: "'uv' 'run' 'helper'", out: fmt.Sprintf(helloAt, "stale")})...)
	err := Run(h.env, h.cfg, Options{Machine: "host-a"})
	if err == nil {
		t.Fatal("a helper that never reports the shipped commit must fail")
	}
	for _, want := range []string{"start the machine's helper", "newhead", "within 10s", "commit stale"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error %q lacks %q", err, want)
		}
	}
	if h.runner.ran("electron") {
		t.Fatal("the app must not open after a failed step")
	}
}

func TestDryRunOnlyReads(t *testing.T) {
	h := newHarness(t, baseFiles(), machineRules("oldhead", "0")...)
	if err := Run(h.env, h.cfg, Options{Machine: "host-a", DryRun: true}); err != nil {
		t.Fatal(err)
	}
	for _, l := range h.runner.log {
		if !strings.Contains(l, "rev-parse") && !strings.Contains(l, "status --porcelain") && !strings.Contains(l, "vibesys-dev-probe") {
			t.Fatalf("a dry run ran %q", l)
		}
	}
	if !strings.Contains(h.out.String(), "push HEAD to the machine") {
		t.Fatalf("plan missing:\n%s", h.out)
	}
}

func TestOptionsAreValidatedBeforeAnythingRuns(t *testing.T) {
	for _, o := range []Options{{Machine: "-oProxyCommand=x"}, {Machine: "a b"}, {Checkout: "/x"}, {Machine: "host-a", Checkout: "relative"}} {
		h := newHarness(t, baseFiles())
		if err := Run(h.env, h.cfg, o); err == nil || len(h.runner.log) != 0 {
			t.Errorf("%+v: err=%v ran=%v", o, err, h.runner.log)
		}
	}
}

func TestUnsavedMachineFailsNamingTheHostsFile(t *testing.T) {
	h := newHarness(t, baseFiles(), machineRules("oldhead", "0")...)
	err := Run(h.env, h.cfg, Options{Machine: "host-b"})
	if err == nil || !strings.Contains(err.Error(), "~/app/hosts.json") || h.runner.ran("vibesys-dev-probe") {
		t.Fatalf("got %v, ran %v", err, h.runner.log)
	}
}

func TestConfigRejectsUnknownKeysAndBadPaths(t *testing.T) {
	for _, bad := range []string{
		testConfig + "\n[extra]\nx = 1\n",
		strings.Replace(testConfig, `client_directory = "clients"`, `client_directory = "/abs"`, 1),
		strings.Replace(testConfig, `ship_ref = "refs/dev/head"`, `ship_ref = "main"`, 1),
		strings.Replace(testConfig, `build_command = ["pnpm", "build"]`, `build_command = []`, 1),
	} {
		if _, err := ParseConfig("dev.toml", bad); err == nil {
			t.Errorf("accepted:\n%s", bad)
		}
	}
}

func localDirty() []rule {
	r := machineRules("oldhead", "0")
	return []rule{r[0], r[1], {contains: "status --porcelain", out: " M x\n"}}
}
