//go:build e2e

package e2e

import (
	"encoding/json"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// useCLI builds vibesys-cli and puts it first on the PATH of everything the rig starts, so the
// scripted agents call it as a real agent would. Its daemon is the user's one per machine; here
// that is the rig's own, in the rig's runtime directory, and the test stops it when it ends.
func (r *rig) useCLI() {
	r.t.Helper()
	bin := filepath.Join(r.dir, "bin")
	if err := os.MkdirAll(bin, 0o755); err != nil {
		r.t.Fatal(err)
	}
	build := exec.Command("go", "build", "-buildvcs=false", "-o", filepath.Join(bin, "vibesys-cli"), "./cmd/vibesys-cli")
	build.Dir = "../../cli"
	if out, err := build.CombinedOutput(); err != nil {
		r.t.Fatalf("build vibesys-cli: %v\n%s", err, out)
	}
	// The rig's daemon is a sandbox daemon: with the runtime directory of its own, it must not meet
	// the one daemon per user that a developer's machine may already run. Whoever starts it, the host
	// or a scripted agent, runs the job tool, so that tool says so itself.
	sandboxed := filepath.Join(bin, "vibesys-cli-sandboxed")
	script := "#!/bin/sh\nVIBESYS_CLI_SANDBOX=1 exec " + filepath.Join(bin, "vibesys-cli") + " \"$@\"\n"
	if err := os.WriteFile(sandboxed, []byte(script), 0o755); err != nil {
		r.t.Fatal(err)
	}
	r.config.JobTool = sandboxed
	r.config.ToolPath = append([]string{bin}, r.config.ToolPath...)
	r.holderEnv = append(r.holderEnv, "VIBESYS_CLI_SANDBOX=1")
	r.env = append(r.env, "VIBESYS_CLI_SANDBOX=1", "PATH="+bin+string(os.PathListSeparator)+r.dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	r.t.Cleanup(func() { r.stopCLIDaemon(filepath.Join(bin, "vibesys-cli")) })
}

// stopCLIDaemon stops the vibesys-cli daemon this rig started and fails the test if any process
// of the rig's vibesys-cli binary is left. The daemon is per user and found through the runtime
// directory in the environment, so the stop must run with the rig's environment: without it the
// stop reaches no daemon (or the developer's own) and the rig's daemon outlives the test.
func (r *rig) stopCLIDaemon(binary string) {
	cmd := exec.Command(binary, "daemon", "stop")
	cmd.Env = r.env
	if out, err := cmd.CombinedOutput(); err != nil {
		r.t.Errorf("vibesys-cli daemon stop: %v\n%s", err, out)
	}
	// The stop returns when the daemon has been told; the process exits a moment later.
	for _, pid := range untilNoneRunning(func() []int { return processesOf(binary) }) {
		r.t.Errorf("a vibesys-cli process (%d) still runs after the test; killing it", pid)
		_ = syscall.Kill(pid, syscall.SIGKILL)
	}
}

// untilNoneRunning calls list until it returns no process and returns what is left when the bound
// passes. The wait ends as soon as nothing is left; the bound only guards against a process that
// never exits.
func untilNoneRunning(list func() []int) []int {
	deadline := time.Now().Add(bound)
	left := list()
	for len(left) > 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
		left = list()
	}
	return left
}

// processTable reads the live executable paths from proc.
func processTable() []proc {
	entries, _ := filepath.Glob("/proc/[0-9]*/exe")
	var table []proc
	for _, entry := range entries {
		target, err := os.Readlink(entry)
		if err != nil {
			continue
		}
		pid, err := strconv.Atoi(strings.Split(entry, "/")[2])
		if err == nil && running(pid) {
			table = append(table, proc{pid, target})
		}
	}
	return table
}

// processesOf lists the live processes running the executable at path.
func processesOf(path string) []int {
	var pids []int
	for _, p := range processTable() {
		if p.exe == path {
			pids = append(pids, p.pid)
		}
	}
	return pids
}

// useMarketplace points the orchestrator at this repository's own plugin marketplace, which is
// what every session loads (the daemon refuses to start a session without one). Nothing is
// installed: the providers are scripted.
func (r *rig) useMarketplace() {
	r.t.Helper()
	root, err := filepath.Abs("../..")
	if err != nil {
		r.t.Fatal(err)
	}
	r.marketplace = root
	r.config.SkillsDirectory = root
}

// actingAgents makes the scripted sidecars act: each agent runs its role's script instead of
// answering "reply N".
func (r *rig) actingAgents() { r.holderEnv = append(r.holderEnv, "VIBESYS_E2E_SCRIPT=act") }

// session is one launched session's directory under the orchestrator's state.
type session struct {
	dir string
	// open is what the session was opened with, as the scripted holder recorded it.
	open struct {
		Skills struct {
			Directory string `json:"directory"`
			Name      string `json:"name"`
		} `json:"skills"`
		AgentDataDirectory string `json:"agentDataDirectory"`
	}
}

func (r *rig) sessions() []session {
	r.t.Helper()
	dirs, err := filepath.Glob(filepath.Join(r.state, "sessions", "*", "[0-9]*"))
	if err != nil {
		r.t.Fatal(err)
	}
	var out []session
	for _, dir := range dirs {
		s := session{dir: dir}
		data, err := os.ReadFile(filepath.Join(dir, "open.json"))
		if err != nil {
			r.t.Fatalf("session %s was not opened: %v", dir, err)
		}
		if err := json.Unmarshal(data, &s.open); err != nil {
			r.t.Fatalf("what opened %s: %v", dir, err)
		}
		out = append(out, s)
	}
	return out
}

// vibesysCLI runs vibesys-cli in the repository as an operator would, and returns its stdout.
func (r *rig) vibesysCLI(repository string, args ...string) string {
	r.t.Helper()
	cmd := exec.Command(filepath.Join(r.dir, "bin", "vibesys-cli"), append(args, "--task", toyTask)...)
	cmd.Dir = repository
	cmd.Env = r.env
	out, err := cmd.Output()
	if err != nil {
		r.t.Fatalf("vibesys-cli %s: %v\n%s", strings.Join(args, " "), err, out)
	}
	return string(out)
}

// measured is the headline number vibesys-cli saved for a commit, or fails if there is none.
func (r *rig) measured(repository, commit string) float64 {
	r.t.Helper()
	var shown struct {
		Results []struct {
			Measure *struct {
				Value float64 `json:"value"`
			} `json:"measure"`
		} `json:"results"`
	}
	if err := json.Unmarshal([]byte(r.vibesysCLI(repository, "show", commit, "--json")), &shown); err != nil {
		r.t.Fatal(err)
	}
	for _, res := range shown.Results {
		if res.Measure != nil {
			return res.Measure.Value
		}
	}
	r.t.Fatalf("vibesys-cli has no measurement of %s", commit)
	return 0
}

// running reports whether a process exists and has not died (a zombie, which no one has reaped
// yet, does not run).
func running(pid int) bool {
	if data, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat"); err == nil {
		fields := strings.Fields(string(data[strings.LastIndexByte(string(data), ')')+1:]))
		return len(fields) > 0 && fields[0] != "Z"
	}
	return alive(pid)
}

// pgids lists the process groups the sidecars recorded for their agents.
func (r *rig) pgids() []int {
	files, _ := filepath.Glob(filepath.Join(r.state, "sessions", "*", "[0-9]*", "agent.pgids"))
	var out []int
	for _, f := range files {
		data, _ := os.ReadFile(f)
		for _, field := range strings.Fields(string(data)) {
			if n, err := strconv.Atoi(field); err == nil {
				out = append(out, n)
			}
		}
	}
	return out
}

// uiWorld is a running world, as a UI test needs to attach to it.
type uiWorld struct {
	// Root and Binary are the daemon's --root and the vibesys-client to run verbs with.
	Root, Binary string
	// Home is the home directory whose .vibesys is Root: a client that finds the gateway by the
	// user's home reaches this world when it runs with HOME set to Home.
	Home string
	// Env is the environment every process of the world was started with.
	Env []string
	// RunID and LeadID name the run, which has reported and not yet been stopped.
	RunID, LeadID string
	Repository    string
	// Task is the task the run optimizes, and Report the headline of the lead's report.
	Task, Report string
	// Agents are the run's agents as the daemon reported them, by the daemon's own enum names.
	Agents []uiAgent
}

// uiAgent is one agent of the run, as the daemon reported it.
type uiAgent struct {
	ID      string `json:"id"`
	Role    string `json:"role"`
	Harness string `json:"harness"`
}

// observeUI is the hook for the UI observer (the Playwright spec of the desktop app, run by
// desktop_ui_test.go when VIBESYS_E2E_DESKTOP_UI=1). When set, the test calls it once the lead has
// reported and before the operator's Stop: the observer attaches to the running world, asserts
// what the Run screen shows against it, presses Stop in the UI instead of the test doing it, and
// returns. Nil when the opt-in is not set, and then the test presses Stop itself.
var observeUI func(t *testing.T, world uiWorld)

func TestTheDesktopPathRunsAnOptimizationEndToEnd(t *testing.T) {
	r := newRig(t)
	r.useCLI()
	r.actingAgents()
	repository := newToy(t, r.dir)

	r.serve()
	const steering = "Prefer Codex workers for volume."
	started := r.receipt("run", "start", "--repository", repository.path, "--task", toyTask, "--brief", steering, "--json")
	if started.RunID == "" || started.AgentID == "" {
		t.Fatalf("run start printed no run and lead: %+v", started)
	}
	w := newWorld()
	w.last = started.Sequence - 1
	r.observe("the lead's report", w, func() bool { return w.report() != "" })

	// The lead and two or more subagents ran, on both harnesses, and the lead's own harness is
	// not the only one.
	lead := w.agents[started.AgentID]
	workers := w.agentsWhere(func(a *agentView) bool { return a.Role == "AGENT_ROLE_WORKER" })
	judges := w.agentsWhere(func(a *agentView) bool { return a.Role == "AGENT_ROLE_JUDGE" })
	if lead == nil || len(workers) != 2 || len(judges) != 1 {
		t.Fatalf("want a lead, two workers and a judge, got %d agents: %+v", len(w.agents), w.agents)
	}
	harnesses := map[string]string{} // plan number to harness
	for _, a := range workers {
		if a.Parent != lead.ID {
			t.Errorf("worker %s is not a subagent of the lead %s", a.ID, lead.ID)
		}
		harnesses[strconv.Itoa(a.Plan)] = a.Harness
	}
	if lead.Harness != "HARNESS_CLAUDE_CODE" || harnesses["1"] != "HARNESS_CLAUDE_CODE" || harnesses["2"] != "HARNESS_CODEX" || judges[0].Harness != "HARNESS_CLAUDE_CODE" {
		t.Errorf("harnesses: lead %s, workers by plan %v, judge %s", lead.Harness, harnesses, judges[0].Harness)
	}

	// Every session loaded the vibesys plugin from the marketplace, in a home of its agent's own.
	homes := map[string]string{} // plugin home to the agent directory that owns it
	for _, sess := range r.sessions() {
		home := sess.open.AgentDataDirectory
		if sess.open.Skills.Name != "vibesys" || sess.open.Skills.Directory != r.marketplace || home == "" {
			t.Errorf("session %s opened with skills %+v in %q, want vibesys from %s with a home", sess.dir, sess.open.Skills, home, r.marketplace)
		}
		agentDir := filepath.Dir(sess.dir)
		if owner, ok := homes[home]; ok && owner != agentDir {
			t.Errorf("agents %s and %s share the plugin home %s", owner, agentDir, home)
		}
		homes[home] = agentDir
		r.checkProviderLoadsOnlyThePlugin(sess)
		if data, err := os.ReadFile(filepath.Join(sess.dir, "peer-failure.txt")); err == nil {
			t.Errorf("a scripted agent broke off: %s", data)
		}
	}
	if len(homes) != len(w.agents) {
		t.Errorf("want one plugin home per agent (%d), got %d: %v", len(w.agents), len(homes), homes)
	}

	// The lead's first message was the rendered lead brief, with this run's values.
	leadDir := filepath.Join(r.state, "sessions", sessionDirOf(r, started.AgentID), "1")
	brief, err := os.ReadFile(filepath.Join(leadDir, "prompts", "1.md"))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"vibesys:optimize", "`" + toyTask + "`", "--fanout " + strconv.Itoa(w.run.FanoutLimit), steering} {
		if !strings.Contains(string(brief), want) {
			t.Errorf("the lead's first message lacks %q:\n%s", want, brief)
		}
	}

	// One brief, three handoffs, and exactly eight job-completion notices wake
	// the lead. Wait for typed delivery acknowledgments before counting files;
	// the report itself may arrive before the last job notice was delivered.
	r.awaitLeadDeliveries(lead.ID)
	const expectedPrompts = 1 + 3 + 8
	if prompts, err := filepath.Glob(filepath.Join(leadDir, "prompts", "*.md")); err != nil || len(prompts) != expectedPrompts {
		t.Errorf("the lead was sent %d prompts, want exactly %d: %v (glob: %v)", len(prompts), expectedPrompts, prompts, err)
	}

	// Each worker's commit was measured by vibesys-cli and the number is the worker's score; the
	// lead promoted the better one, and the number it reported is the one the CLI has for the head.
	commits := map[int]string{}
	for _, m := range w.messages {
		if m.Handoff == nil || m.Handoff.Commit == "" {
			continue
		}
		sender := w.agents[m.From]
		if sender == nil || sender.Role != "AGENT_ROLE_WORKER" || sender.Plan != m.Handoff.PlanNumber || m.To != lead.ID {
			t.Errorf("handoff %+v is not a worker's own to the lead (sender %+v)", m.Handoff, sender)
		}
		commits[m.Handoff.PlanNumber] = m.Handoff.Commit
	}
	scores := map[int]float64{1: 14, 2: 17}
	for plan, score := range scores {
		got := r.measured(repository.path, commits[plan])
		// Three samples report score+0.00, +0.01 and +0.02.
		if math.Abs(got-(score+0.01)) > 1e-9 {
			t.Errorf("plan %d: vibesys-cli measured %v for %s, want %v", plan, got, commits[plan], score+0.01)
		}
	}
	var current struct {
		Current struct {
			Head   string `json:"head"`
			Result struct {
				Measure struct {
					Value float64 `json:"value"`
				} `json:"measure"`
			} `json:"result"`
		} `json:"current"`
	}
	if err := json.Unmarshal([]byte(r.vibesysCLI(repository.path, "result", "--json")), &current); err != nil {
		t.Fatal(err)
	}
	if current.Current.Head != commits[2] {
		t.Errorf("the result branch is at %s, want the Codex worker's %s", current.Current.Head, commits[2])
	}
	reported := regexp.MustCompile(`^best score ([0-9.]+) at ([0-9a-f]{40})$`).FindStringSubmatch(w.report())
	if reported == nil {
		t.Fatalf("the lead's report headline %q is not 'best score N at COMMIT'", w.report())
	}
	if value, _ := strconv.ParseFloat(reported[1], 64); value != current.Current.Result.Measure.Value || reported[2] != current.Current.Head {
		t.Errorf("the report says %s at %s, vibesys-cli says %v at %s", reported[1], reported[2], current.Current.Result.Measure.Value, current.Current.Head)
	}

	t.Logf("run %s: lead %s, workers %s (plan %s) and %s (plan %s), judge %s",
		started.RunID, lead.ID, workers[0].ID, strconv.Itoa(workers[0].Plan), workers[1].ID, strconv.Itoa(workers[1].Plan), judges[0].ID)
	t.Logf("result branch at %s with score %v; the lead reported %q", current.Current.Head, current.Current.Result.Measure.Value, w.report())

	if observeUI != nil {
		var agents []uiAgent
		for _, a := range w.agents {
			agents = append(agents, uiAgent{ID: a.ID, Role: a.Role, Harness: a.Harness})
		}
		sort.Slice(agents, func(i, j int) bool { return agents[i].ID < agents[j].ID })
		observeUI(t, uiWorld{
			Root: r.root, Binary: r.clientBinary, Home: r.dir, Env: r.env,
			RunID: started.RunID, LeadID: started.AgentID, Repository: repository.path,
			Task: toyTask, Report: w.report(), Agents: agents,
		})
	}

	// Stop ends the run: every agent ended, and the agent process groups the sidecars recorded
	// are gone. A sidecar whose session closed stays up for a while so the daemon can read the
	// close, then exits by itself (vs_sidecar.main.LINGER_SECONDS), so it is not asserted dead;
	// what is asserted is that none of them still holds an agent. The jobs vibesys-cli ran for the
	// scripted agents were all awaited by them, so none is left queued or running.
	pgids := r.pgids()
	if observeUI == nil {
		r.client("run", "stop", started.RunID, "--yes")
	}
	r.observe("the run ended", w, func() bool { return w.run.Status == "RUN_STATUS_ENDED" })
	for _, a := range w.agents {
		if a.Status != "AGENT_STATUS_ENDED" {
			t.Errorf("agent %s (%s) is %s after Stop", a.ID, a.Role, a.Status)
		}
	}
	for _, pgid := range pgids {
		if running(pgid) {
			t.Errorf("agent process group %d still runs after Stop", pgid)
		}
	}
	if left := r.pgids(); len(left) != 0 {
		t.Errorf("sidecars still record agent process groups after Stop: %v", left)
	}
	// A watch from the start of the run ends in the state the live feed reached.
	replay := newWorld()
	replay.last = started.Sequence - 1
	r.observe("the replay of the whole run", replay, func() bool { return replay.last >= w.last })
	if !reflect.DeepEqual(replay.agents, w.agents) || !reflect.DeepEqual(replay.run, w.run) || !reflect.DeepEqual(replay.messages, w.messages) {
		t.Errorf("the replay differs from the live feed:\nlive   %+v\nreplay %+v", w, replay)
	}
	var jobs struct {
		Jobs []struct {
			ID    string `json:"id"`
			State string `json:"state"`
		} `json:"jobs"`
	}
	if err := json.Unmarshal([]byte(r.vibesysCLI(repository.path, "jobs", "--json")), &jobs); err != nil {
		t.Fatal(err)
	}
	if len(jobs.Jobs) == 0 {
		t.Error("vibesys-cli ran no job for the run")
	}
	for _, j := range jobs.Jobs {
		if j.State != "JOB_STATE_SUCCEEDED" {
			t.Errorf("job %s is %s after Stop, want succeeded", j.ID, j.State)
		}
	}
}

// sessionDirOf is the directory name the orchestrator keeps an agent's sessions under.
func sessionDirOf(r *rig, agent string) string {
	for _, sess := range r.sessions() {
		data, err := os.ReadFile(filepath.Join(sess.dir, "open.json"))
		if err == nil && strings.Contains(string(data), `"`+agent+`"`) {
			return filepath.Base(filepath.Dir(sess.dir))
		}
	}
	r.t.Fatalf("no session of agent %s", agent)
	return ""
}

// checkProviderLoadsOnlyThePlugin reads what the real driver asked the scripted provider to
// start: the plugin, enabled in the agent's own home, and none of the operator's configuration.
func (r *rig) checkProviderLoadsOnlyThePlugin(sess session) {
	r.t.Helper()
	data, err := os.ReadFile(filepath.Join(sess.dir, "spawns.json"))
	if err != nil {
		r.t.Fatalf("session %s recorded no provider start: %v", sess.dir, err)
	}
	var spawned struct {
		Provider string `json:"provider"`
		Spawns   []struct {
			Argv []string `json:"argv"`
			Home string   `json:"home"`
		} `json:"spawns"`
		Commands [][]string `json:"commands"`
	}
	if err := json.Unmarshal(data, &spawned); err != nil {
		r.t.Fatal(err)
	}
	if len(spawned.Spawns) != 1 {
		r.t.Fatalf("session %s started the provider %d times", sess.dir, len(spawned.Spawns))
	}
	spawn := spawned.Spawns[0]
	if spawn.Home != sess.open.AgentDataDirectory {
		r.t.Errorf("session %s: the provider's config home is %q, want the plugin home %q", sess.dir, spawn.Home, sess.open.AgentDataDirectory)
	}
	flag := func(name string) []string {
		var values []string
		for i, a := range spawn.Argv {
			if a == name && i+1 < len(spawn.Argv) {
				values = append(values, spawn.Argv[i+1])
			}
		}
		return values
	}
	switch spawned.Provider {
	case "claude":
		if got := flag("--setting-sources"); len(got) != 1 || got[0] != "project,local" {
			r.t.Errorf("session %s: claude reads setting sources %v, want only project,local", sess.dir, got)
		}
		var settings struct {
			Enabled map[string]bool `json:"enabledPlugins"`
		}
		if got := flag("--settings"); len(got) != 1 || json.Unmarshal([]byte(got[0]), &settings) != nil || len(settings.Enabled) != 1 || !settings.Enabled["vibesys@vibesys"] {
			r.t.Errorf("session %s: claude settings %v do not enable exactly vibesys@vibesys", sess.dir, got)
		}
	case "codex":
		want := `plugins={"vibesys@vibesys" = {enabled = true}}`
		found := false
		for _, c := range flag("--config") {
			found = found || c == want
		}
		if !found {
			r.t.Errorf("session %s: codex is not configured with %s: %v", sess.dir, want, flag("--config"))
		}
	default:
		r.t.Errorf("session %s: unknown provider %q", sess.dir, spawned.Provider)
	}
	var added, installed bool
	for _, c := range spawned.Commands {
		line := strings.Join(c[1:], " ")
		added = added || line == "plugin marketplace add "+r.marketplace
		// Claude installs with `plugin install`, Codex with `plugin add`.
		installed = installed || ((strings.HasPrefix(line, "plugin install ") || strings.HasPrefix(line, "plugin add ")) && strings.HasSuffix(line, "vibesys@vibesys"))
	}
	if !added || !installed {
		r.t.Errorf("session %s: the plugin was not added and installed before the provider started: %v", sess.dir, spawned.Commands)
	}
}
