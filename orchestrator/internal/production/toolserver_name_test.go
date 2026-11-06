package production

import (
	"os"
	"regexp"
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
	doc, err := os.ReadFile("../../../skills/search/optimize/references/harness/vibesys-orchestrator.md")
	if err != nil {
		t.Fatal(err)
	}
	names := toolServerNames(doc)
	if len(names) == 0 {
		t.Fatal("the harness skill no longer says which tool server carries the tools; the name is not held")
	}
	for _, name := range names {
		if name != ToolServerName {
			t.Errorf("the harness skill names the tool server %q, but the host registers %q", name, ToolServerName)
		}
	}
}
