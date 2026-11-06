package production

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"buf.build/go/protovalidate"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

func marketplace(t *testing.T, manifest string) string {
	t.Helper()
	root := t.TempDir()
	dir := filepath.Join(root, ".claude-plugin")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "marketplace.json"), []byte(manifest), 0o644); err != nil {
		t.Fatal(err)
	}
	return root
}

const good = `{"name": "vibesys", "plugins": [{"name": "other"}, {"name": "vibesys", "source": "./skills"}]}`

// A marketplace setting that is missing or relative is refused with the setting named, when the
// daemon starts. Nothing here reads what the marketplace holds: the harness is its one reader, so
// one it cannot load comes back as the session's start failure.
func TestAWrongMarketplaceIsRefusedNamingTheSetting(t *testing.T) {
	cases := map[string]struct{ path, want string }{
		"unset":    {"", "names no"},
		"relative": {"skills", "not an absolute path"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := PluginFor(c.path, developerBuild)
			if err == nil || !strings.Contains(err.Error(), SettingSkills) || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("PluginFor(%q) = %v, want an error naming %s and %q", c.path, err, SettingSkills, c.want)
			}
		})
	}
}

func TestTheMarketplaceNamesTheVibesysPlugin(t *testing.T) {
	root := marketplace(t, good)
	got, err := PluginFor(root+"/", developerBuild)
	if err != nil {
		t.Fatal(err)
	}
	if want := (Plugin{Marketplace: root, Name: "vibesys"}); got != want {
		t.Fatalf("plugin = %+v, want %+v", got, want)
	}
}

// Without a marketplace the launcher refuses every launch and says why: no session starts without
// the plugin.
func TestWithoutAMarketplaceEveryLaunchIsRefused(t *testing.T) {
	sidecars, err := SidecarsFor(Parts{Config: &pb.HostConfig{SessionHolderCommand: "true"}, Find: developerBuild}, t.TempDir(), "/x/bin/vibesys-host", slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	_, err = sidecars.Launch(context.Background(), daemon.SessionStart{})
	if err == nil || !strings.Contains(err.Error(), SettingSkills) {
		t.Fatalf("Launch = %v, want a refusal naming %s", err, SettingSkills)
	}
}

func launcher(t *testing.T) (*ProcessSidecars, Plugin) {
	t.Helper()
	plugin, err := PluginFor(marketplace(t, good), developerBuild)
	if err != nil {
		t.Fatal(err)
	}
	return &ProcessSidecars{Root: t.TempDir(), Executable: "/x/bin/vibesys-host", Log: slog.New(slog.DiscardHandler)}, plugin
}

func lead(agent core.AgentID, session uint32) daemon.SessionStart {
	start := sessionStart(string(agent), core.RoleLead, "/repo")
	start.Launch.Session.N = session
	start.Tools.Session = session
	return start
}

// The request carries the skills, and the holder's own validation accepts it.
func TestTheOpenRequestCarriesTheSkillsAndIsValid(t *testing.T) {
	s, plugin := launcher(t)
	request, err := s.openRequest(lead("a1", 1), plugin)
	if err != nil {
		t.Fatal(err)
	}
	if got := request.GetSkills(); got.GetDirectory() != plugin.Marketplace || got.GetName() != "vibesys" {
		t.Fatalf("skills = %v", got)
	}
	if err := protovalidate.Validate(request); err != nil {
		t.Fatalf("the open request is invalid: %v", err)
	}
}

// A resume (a later session of the agent) finds the directory the first session installed into, and
// another agent never shares it.
func TestAnAgentKeepsOneDataDirectoryAcrossSessions(t *testing.T) {
	s, plugin := launcher(t)
	home := func(agent core.AgentID, session uint32) string {
		request, err := s.openRequest(lead(agent, session), plugin)
		if err != nil {
			t.Fatal(err)
		}
		return request.GetAgentDataDirectory()
	}
	if first, again := home("a1", 1), home("a1", 2); first != again {
		t.Fatalf("session 2 got directory %s, session 1 had %s", again, first)
	}
	if home("a1", 1) == home("a2", 1) {
		t.Fatal("two agents share a data directory")
	}
}
