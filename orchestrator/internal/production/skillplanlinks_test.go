package production

import (
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// vibesysd refuses a measure from a run that names neither --plan nor --no-plan, so a command the
// search skills tell an agent to run must name one of them, or the agent learns the rule from a
// refusal. A command is the text after "submit measure" to the end of its code span or its line.
var measureCommand = regexp.MustCompile("submit measure[^`\n]*")

func TestEveryMeasureCommandInTheSearchSkillsSaysWhatPlanItTests(t *testing.T) {
	var commands int
	err := filepath.WalkDir(skillsRoot, func(path string, entry fs.DirEntry, err error) error {
		if err != nil || entry.IsDir() || !strings.HasSuffix(path, ".md") {
			return err
		}
		text, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		for _, command := range measureCommand.FindAllString(string(text), -1) {
			commands++
			if !strings.Contains(command, "--plan") && !strings.Contains(command, "--no-plan") {
				t.Errorf("%s tells an agent to run %q, which names neither --plan <n> nor --no-plan", path, command)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if commands == 0 {
		t.Error("the skills no longer say how to submit a measure; this check cannot pass over an emptied directory")
	}
}
