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
	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
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

func env(path string) func(string) string {
	return func(k string) string {
		if k == EnvMarketplace {
			return path
		}
		return ""
	}
}

// A marketplace setting that is missing, relative, not a checkout or without the plugin is refused
// with the setting named, when the daemon starts.
func TestAWrongMarketplaceIsRefusedNamingTheSetting(t *testing.T) {
	cases := map[string]struct{ path, want string }{
		"unset":      {"", "is not set"},
		"relative":   {"skills", "not an absolute path"},
		"no such":    {filepath.Join(t.TempDir(), "nowhere"), "not a marketplace checkout"},
		"not json":   {marketplace(t, "{"), "not a marketplace manifest"},
		"no plugin":  {marketplace(t, `{"plugins": [{"name": "other"}]}`), "lists no plugin named vibesys"},
		"no plugins": {marketplace(t, `{}`), "lists no plugin named vibesys"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := PluginFor(env(c.path))
			if err == nil || !strings.Contains(err.Error(), EnvMarketplace) || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("PluginFor(%q) = %v, want an error naming %s and %q", c.path, err, EnvMarketplace, c.want)
			}
		})
	}
}

func TestTheMarketplaceNamesTheVibesysPlugin(t *testing.T) {
	root := marketplace(t, good)
	got, err := PluginFor(env(root + "/"))
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
	sidecars, err := SidecarsFor(func(k string) string {
		if k == EnvSidecar {
			return "true"
		}
		return ""
	}, t.TempDir(), "vibesys-host", slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	_, err = sidecars.Launch(context.Background(), core.LaunchSession{}, nil)
	if err == nil || !strings.Contains(err.Error(), EnvMarketplace) {
		t.Fatalf("Launch = %v, want a refusal naming %s", err, EnvMarketplace)
	}
}

func launcher(t *testing.T) *ProcessSidecars {
	t.Helper()
	plugin, err := PluginFor(env(marketplace(t, good)))
	if err != nil {
		t.Fatal(err)
	}
	return &ProcessSidecars{Root: t.TempDir(), Plugin: plugin}
}

func lead(agent core.AgentID, session uint32) core.LaunchSession {
	return core.LaunchSession{
		Session: core.Session{Agent: agent, N: session}, Run: "run", Repository: "/repo", Task: "task", Role: core.RoleLead,
		Spec: core.AgentSpec{Harness: core.ClaudeCode},
	}
}

// The launch file carries the plugin and survives the JSON the sidecar reads, and the sidecar's own
// validation accepts it.
func TestTheLaunchFileCarriesThePluginAndIsValid(t *testing.T) {
	s := launcher(t)
	spec, err := s.launchSpec(context.Background(), lead("a1", 1))
	if err != nil {
		t.Fatal(err)
	}
	data, err := protojson.Marshal(spec)
	if err != nil {
		t.Fatal(err)
	}
	var back pb.LaunchSession
	if err := protojson.Unmarshal(data, &back); err != nil {
		t.Fatal(err)
	}
	if got := back.GetPlugin(); got.GetMarketplace() != s.Plugin.Marketplace || got.GetName() != "vibesys" || got.GetHome() == "" {
		t.Fatalf("plugin = %v", got)
	}
	if err := protovalidate.Validate(&back); err != nil {
		t.Fatalf("the launch file is invalid: %v", err)
	}
}

// A resume (a later session of the agent) finds the home the first session installed into, and
// another agent never shares it.
func TestAnAgentKeepsOnePluginHomeAcrossSessions(t *testing.T) {
	s := launcher(t)
	home := func(agent core.AgentID, session uint32) string {
		spec, err := s.launchSpec(context.Background(), lead(agent, session))
		if err != nil {
			t.Fatal(err)
		}
		return spec.GetPlugin().GetHome()
	}
	if first, again := home("a1", 1), home("a1", 2); first != again {
		t.Fatalf("session 2 got home %s, session 1 had %s", again, first)
	}
	if home("a1", 1) == home("a2", 1) {
		t.Fatal("two agents share a plugin home")
	}
}
