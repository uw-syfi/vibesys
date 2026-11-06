package production

import (
	"encoding/json"
	"os"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// environmentGolden is the one list of the names an agent's processes get, which the vibesys-cli's
// tests read as well.
const environmentGolden = "../../../proto/vibesys/cli/v1/conformance/identity_env.golden.json"

// fieldFor fills what the golden names, each with its own value.
var fieldFor = map[string]func(*daemon.SessionStart, string){
	"repository":                   func(s *daemon.SessionStart, v string) { s.Repository = v },
	"task":                         func(s *daemon.SessionStart, v string) { s.Task = v },
	"run_id":                       func(s *daemon.SessionStart, v string) { s.Launch.Run = core.RunID(v) },
	"session.agent_id":             func(s *daemon.SessionStart, v string) { s.Launch.Session.Agent = core.AgentID(v) },
	"cli_location.cli":             func(s *daemon.SessionStart, v string) { s.Location.CLI = v },
	"cli_location.xdg_runtime_dir": func(s *daemon.SessionStart, v string) { s.Location.RuntimeDir = v },
	"cli_location.xdg_state_home":  func(s *daemon.SessionStart, v string) { s.Location.StateHome = v },
	"cli_location.home":            func(s *daemon.SessionStart, v string) { s.Location.Home = v },
}

// The environment built here is the golden's, name for name, and an empty value does what it says:
// unset removes the name, and keep leaves what the login environment holds.
func TestTheAgentEnvironmentIsTheGoldenList(t *testing.T) {
	data, err := os.ReadFile(environmentGolden)
	if err != nil {
		t.Fatal(err)
	}
	var golden struct {
		Names []struct {
			Name  string `json:"name"`
			From  string `json:"from"`
			Empty string `json:"empty"`
		} `json:"names"`
	}
	if err := json.Unmarshal(data, &golden); err != nil {
		t.Fatal(err)
	}
	full := daemon.SessionStart{Launch: core.LaunchSession{
		Session: core.Session{Agent: "a", N: 3}, Spec: core.AgentSpec{Harness: core.ClaudeCode},
	}}
	for from, fill := range fieldFor {
		fill(&full, "value of "+from)
	}
	filled := agentEnvironment(full)
	if len(filled.GetUnset()) != 0 {
		t.Fatalf("names unset with everything given: %v", filled.GetUnset())
	}
	var empty daemon.SessionStart
	blank := agentEnvironment(empty)
	want := map[string]bool{}
	for _, name := range golden.Names {
		if name.From == "" {
			if _, set := filled.GetSet()[name.Name]; set {
				t.Errorf("%s is one the orchestrator does not fill, and it does", name.Name)
			}
			continue
		}
		want[name.Name] = true
		got, set := filled.GetSet()[name.Name]
		switch {
		case !set:
			t.Errorf("%s is missing", name.Name)
		case fieldFor[name.From] != nil && got != "value of "+name.From:
			t.Errorf("%s is %q, want it from %s", name.Name, got, name.From)
		}
		if unset := slices.Contains(blank.GetUnset(), name.Name); unset != (name.Empty == "unset") {
			t.Errorf("%s: unset when empty is %v, the golden says %s", name.Name, unset, name.Empty)
		}
	}
	for name := range filled.GetSet() {
		if !want[name] {
			t.Errorf("%s is not in the golden", name)
		}
	}
}
