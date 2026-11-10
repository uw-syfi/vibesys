package production

import (
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// skillsRoot is the search skills the agents run, which are copied into the agent's workspace.
const skillsRoot = "../../../skills/search"

// namesOfOthers are the VIBESYS_ names the search skills use that no message of the run declares,
// each with its owner. The installer's settings are read by POSIX sh before any binary of ours exists
// (docs/contributing/process-boundaries.md), so it stays a plain name.
var namesOfOthers = map[string]string{
	"VIBESYS_CLI_INSTALL_DIR":  "the installer's own setting, where it puts vibesys-cli",
	"VIBESYS_CLI_VERSION":      "the installer's own setting, the version it installs instead of the newest",
	"VIBESYS_CLI_RELEASES_URL": "the installer's own setting, the mirror it reads release assets from",
	"VIBESYS_K8S_REGISTRY":     "a setting of a task's own Kubernetes evaluator, which its author documents in the task format",
	"VIBESYS_K8S_BUILDER":      "a setting of a task's own Kubernetes evaluator, which its author documents in the task format",
}

// Every VIBESYS_ name the search skills tell an agent to read is a field of a message the
// orchestrator writes, or of the environment vibesys-cli gives a task's commands (JobEnvironment),
// which the task format tells a task's author to read, so the skills and the environment cannot drift: renaming a field fails here
// before an agent finds a variable unset. The two names the skills run most, the run and the CLI,
// must still be in the skills, so this test cannot pass over an emptied directory.
func TestTheSearchSkillsNameOnlyDeclaredVariables(t *testing.T) {
	declared := map[string]bool{}
	for _, m := range []proto.Message{&cliv1.AgentContext{}, &cliv1.OperatorContext{}, &cliv1.SkillEnvironment{}, &cliv1.JobEnvironment{}, &pb.ToolServerCredentials{}} {
		for _, name := range names(t, m) {
			declared[name] = true
		}
	}
	token := regexp.MustCompile(`VIBESYS_[A-Z0-9_]*[A-Z0-9]`)
	seen := map[string]bool{}
	err := filepath.WalkDir(skillsRoot, func(path string, entry fs.DirEntry, err error) error {
		if err != nil || entry.IsDir() || !strings.HasSuffix(path, ".md") {
			return err
		}
		text, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		for _, name := range token.FindAllString(string(text), -1) {
			seen[name] = true
			if !declared[name] && namesOfOthers[name] == "" {
				t.Errorf("%s names %s, which no message declares", path, name)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"VIBESYS_RUN", "VIBESYS_CLI"} {
		if !seen[name] {
			t.Errorf("the skills no longer read $%s; the shell reads of the run and the CLI are the contract", name)
		}
	}
	for name := range namesOfOthers {
		if !seen[name] {
			t.Errorf("%s is no longer in the skills; delete its entry", name)
		}
	}
}
