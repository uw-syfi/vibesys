package hostconfig_test

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"testing/quick"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
)

// What a host's environment is depends on its home, its configuration and the session facts alone.
// Whatever else the environment of its starter holds, retired settings first of all, is not there.
func TestAHostsEnvironmentIsNotInherited(t *testing.T) {
	config := &pb.HostConfig{ToolPath: []string{"/tools/bin", "/usr/bin"}}
	if err := quick.Check(func(junk map[string]string) bool {
		for _, setting := range hostconfig.Retired {
			junk[setting.Name] = "/nonsense"
		}
		junk["VIBESYS_HOME"], junk["PYTHONPATH"], junk["PATH"], junk["HOME"] = "/nonsense", "/nonsense", "/nonsense", "/nonsense"
		for _, fact := range []string{"USER", "LOGNAME", "SHELL", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_STATE_HOME"} {
			delete(junk, fact)
		}
		env := hostconfig.Environ(config, "/home/me", func(name string) string { return junk[name] })
		return slices.Equal(env, []string{"HOME=/home/me", "PATH=/tools/bin:/usr/bin"})
	}, nil); err != nil {
		t.Fatal(err)
	}
	got := hostconfig.Environ(nil, "/home/me", func(name string) string {
		if name == "USER" {
			return "me"
		}
		return ""
	})
	if !slices.Contains(got, "USER=me") || !strings.HasPrefix(got[1], "PATH=/home/me/.local/bin:") || !strings.Contains(got[1], "/usr/bin") {
		t.Fatalf("with no configuration: %q", got)
	}
}

// A configuration survives saving and loading exactly, and its digest names what it says: equal
// configurations have one digest, different ones have different digests, and no configuration at all,
// saved or never written, has the empty digest.
func TestAConfigurationRoundTripsAndItsDigestNamesIt(t *testing.T) {
	root := t.TempDir()
	if config, err := hostconfig.Load(root); err != nil || hostconfig.Digest(config) != "" {
		t.Fatalf("no file: %v, %v", config, err)
	}
	seen := map[string]bool{"": true}
	for _, config := range []*pb.HostConfig{
		{StateDirectory: "/s"}, {StateDirectory: "/t"}, {SessionHolderCommand: "env A=B run"}, {SkillsDirectory: "/s"},
		{JobTool: "/j"}, {ToolPath: []string{"/a", "/b"}}, {ToolPath: []string{"/b", "/a"}},
	} {
		if err := hostconfig.Save(root, config); err != nil {
			t.Fatal(err)
		}
		loaded, err := hostconfig.Load(root)
		if err != nil {
			t.Fatal(err)
		}
		digest := hostconfig.Digest(loaded)
		if digest != hostconfig.Digest(config) || seen[digest] {
			t.Errorf("%v: digest %q after a round trip, already seen %v", config, digest, seen[digest])
		}
		seen[digest] = true
	}
	if err := hostconfig.Save(root, &pb.HostConfig{}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(hostconfig.Path(root)); !os.IsNotExist(err) {
		t.Fatalf("the empty configuration left a file: %v", err)
	}
}

// An unknown key names itself, a relative path names its field, and neither is taken for anything.
func TestAConfigurationThatDoesNotReadIsRefusedNamingWhy(t *testing.T) {
	root := t.TempDir()
	for data, want := range map[string]string{
		`{"stateDirectry": "/s"}`:        "stateDirectry",
		`{"stateDirectory": "relative"}`: "state_directory",
		`{"skillsDirectory": "rel"}`:     "skills_directory",
		`{"jobTool": "rel"}`:             "job_tool",
		`{"toolPath": ["rel"]}`:          "tool_path",
		`not json`:                       "host-config.json",
	} {
		if err := os.WriteFile(hostconfig.Path(root), []byte(data), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := hostconfig.Load(root); err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: %v, want an error naming %s", data, err, want)
		}
	}
	if err := hostconfig.Save(root, &pb.HostConfig{JobTool: "relative"}); err == nil {
		t.Error("a relative job tool was saved")
	}
}

// Save replaces the file in one step and leaves nothing beside it.
func TestSaveLeavesOneFile(t *testing.T) {
	root := filepath.Join(t.TempDir(), "made", "for", "it")
	if err := hostconfig.Save(root, &pb.HostConfig{JobTool: "/j"}); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(root)
	if err != nil || len(entries) != 1 {
		t.Fatalf("%v, %v", entries, err)
	}
}

// Finding: the host's cleared environment dropped what the job tool reads, so a daemon the host started
// ignored the operator's Slurm and Docker settings. From an environment that sets everything to
// something, only what is named survives; each of the job tool's settings is among it.
func TestEnvironKeepsWhatTheJobToolReadsAndNothingElse(t *testing.T) {
	jobTool := map[string]bool{"VIBESYS_CLI_CONFIG": true, "XDG_CONFIG_HOME": true, "XDG_CACHE_HOME": true, "XDG_RUNTIME_DIR": true, "XDG_STATE_HOME": true}
	session := map[string]bool{"USER": true, "LOGNAME": true, "SHELL": true, "TMPDIR": true}
	hostile := func(name string) string { return "/hostile/" + name }
	env := hostconfig.Environ(&pb.HostConfig{ToolPath: []string{"/a"}}, "/home/u", hostile)
	got := map[string]string{}
	for _, entry := range env {
		name, value, _ := strings.Cut(entry, "=")
		got[name] = value
	}
	if got["HOME"] != "/home/u" || got["PATH"] != "/a" {
		t.Errorf("the host's own: %v", got)
	}
	delete(got, "HOME")
	delete(got, "PATH")
	for name := range jobTool {
		if got[name] != hostile(name) {
			t.Errorf("%s = %q, want it kept", name, got[name])
		}
	}
	for name := range got {
		if !jobTool[name] && !session[name] {
			t.Errorf("%s came through from whoever started the host", name)
		}
	}
	// A setting that is not set stays not set.
	for _, entry := range hostconfig.Environ(nil, "/home/u", func(string) string { return "" }) {
		if name, _, _ := strings.Cut(entry, "="); name != "HOME" && name != "PATH" {
			t.Errorf("%s was made up", name)
		}
	}
}

// Check names the field of what is not there, and an empty field is not looked at.
func TestCheckNamesTheFieldOfWhatIsNotThere(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "plain")
	tool := filepath.Join(dir, "tool")
	if err := os.WriteFile(file, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(tool, nil, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		config *pb.HostConfig
		want   string
	}{
		{&pb.HostConfig{}, ""},
		{&pb.HostConfig{SkillsDirectory: dir, JobTool: tool}, ""},
		{&pb.HostConfig{SkillsDirectory: filepath.Join(dir, "gone")}, "skills_directory"},
		{&pb.HostConfig{SkillsDirectory: file}, "skills_directory"},
		{&pb.HostConfig{JobTool: filepath.Join(dir, "gone")}, "job_tool"},
		{&pb.HostConfig{JobTool: file}, "job_tool"},
		{&pb.HostConfig{JobTool: dir}, "job_tool"},
	} {
		err := hostconfig.Check(c.config)
		if (c.want == "") != (err == nil) || (err != nil && !strings.Contains(err.Error(), c.want)) {
			t.Errorf("%v: %v, want %q", c.config, err, c.want)
		}
	}
}
