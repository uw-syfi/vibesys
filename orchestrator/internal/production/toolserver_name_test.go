package production

import (
	"os"
	"regexp"
	"strings"
	"testing"
)

var namedToolServer = regexp.MustCompile("`([a-z-]+)` tool server")

// toolServerNames lists the tool server names a harness doc gives.
func toolServerNames(doc []byte) []string {
	var names []string
	for _, m := range namedToolServer.FindAllSubmatch(doc, -1) {
		names = append(names, string(m[1]))
	}
	return names
}

func harnessDoc(t *testing.T) string {
	t.Helper()
	doc, err := os.ReadFile("../../../skills/search/optimize/references/harness/vibesys-orchestrator.md")
	if err != nil {
		t.Fatal(err)
	}
	return string(doc)
}

func markdownSection(doc, heading string) string {
	start := strings.Index(doc, "## "+heading)
	if start < 0 {
		return ""
	}
	section := doc[start+len("## "+heading):]
	if end := strings.Index(section, "\n## "); end >= 0 {
		section = section[:end]
	}
	return strings.ToLower(strings.Join(strings.Fields(section), " "))
}

func containsOneOf(text string, alternatives ...string) bool {
	for _, alternative := range alternatives {
		if strings.Contains(text, alternative) {
			return true
		}
	}
	return false
}

func TestToolServerNamesAreReadFromTheDoc(t *testing.T) {
	if got := toolServerNames([]byte("through the `vibesys-orchestrator` tool server, and `vibesys` tool server")); len(got) != 2 || got[0] != "vibesys-orchestrator" {
		t.Fatalf("got %v", got)
	}
	if got := toolServerNames([]byte("no server named here")); len(got) != 0 {
		t.Fatalf("got %v", got)
	}
}

// The harness skill tells an agent which tool server carries its tools. When the doc names another
// server than the one the host registers, the agent looks for tools that are not there.
func TestTheHarnessSkillNamesTheToolServerTheHostRegisters(t *testing.T) {
	names := toolServerNames([]byte(harnessDoc(t)))
	if len(names) == 0 {
		t.Fatal("the harness skill no longer says which tool server carries the tools; the name is not held")
	}
	for _, name := range names {
		if name != ToolServerName {
			t.Errorf("the harness skill names the tool server %q, but the host registers %q", name, ToolServerName)
		}
	}
}

// The core turns a final job update into one message that starts a lead turn, even when the
// level-triggered source repeats that update. The harness instructions must not tell the lead to
// replace that mechanism with polling.
func TestTheHarnessSkillDescribesTheJobCompletionWake(t *testing.T) {
	section := markdownSection(harnessDoc(t), "Run a long command as a tracked background task")
	if section == "" {
		t.Fatal("the harness skill no longer has its tracked-background-task instructions")
	}
	concepts := []struct {
		name    string
		present bool
	}{
		{"job completion", containsOneOf(section, "job finish", "job end", "job settle", "job in a final state")},
		{"lead wake", containsOneOf(section, "wake", "notif")},
		{"deduplication", containsOneOf(section, "once", "repeated", "duplicate")},
	}
	for _, concept := range concepts {
		if !concept.present {
			t.Errorf("the harness skill's tracked-background-task instructions omit the %s contract", concept.name)
		}
	}
	if containsOneOf(section, "nothing notifies", "no notification", "never wakes") {
		t.Error("the harness skill contradicts the orchestrator's job completion wake")
	}
}
