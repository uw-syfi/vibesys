package mcp_test

import (
	"math/rand/v2"
	"regexp"
	"strings"
	"testing"

	"google.golang.org/protobuf/reflect/protoreflect"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// The tool sends DeclareTask for the caller's own run with the name it was given, whatever the name,
// and says the directory the task's files go in.
func TestDeclareTaskSendsTheCommandForTheCallersRunWithTheGivenName(t *testing.T) {
	r := rand.New(rand.NewPCG(41, 42))
	for range 200 {
		name := matching(r, taskNamePattern)
		d := &fakeDaemon{}
		text, isErr := call(t, newServer(core.RoleLead, d), 1, "declare_task", map[string]any{"task": name})
		if isErr || !strings.Contains(text, ".vibesys/tasks/"+name) {
			t.Fatalf("declare_task %q: error=%v %q", name, isErr, text)
		}
		got := d.sent[0].GetDeclareTask()
		if len(d.sent) != 1 || got.GetRunId() != "r-1" || got.GetTask() != name {
			t.Fatalf("declare_task %q sent %v", name, d.sent)
		}
	}
}

// A name that breaks the task-name rule is refused at the tool, naming the field, and sends nothing.
func TestDeclareTaskRefusesANameThatBreaksTheRuleNamingTheField(t *testing.T) {
	re := regexp.MustCompile(taskNamePattern)
	r := rand.New(rand.NewPCG(43, 44))
	alphabet := []rune("ab9_-. /\\é\n$~")
	bad := []string{"", ".hidden", "a/b", "../x", "with space", "é", strings.Repeat("a", 129)}
	for range 300 {
		n := make([]rune, 1+r.IntN(8))
		for i := range n {
			n[i] = alphabet[r.IntN(len(alphabet))]
		}
		if name := string(n); !re.MatchString(name) {
			bad = append(bad, name)
		}
	}
	for _, name := range bad {
		d := &fakeDaemon{}
		text, isErr := call(t, newServer(core.RoleLead, d), 1, "declare_task", map[string]any{"task": name})
		if !isErr || !strings.Contains(text, "`task`") || len(d.sent) != 0 {
			t.Fatalf("declare_task %q: error=%v sent=%d %q", name, isErr, len(d.sent), text)
		}
	}
}

// Only the lead has the tool: any other role is told so, and nothing is sent.
func TestOnlyTheLeadMayDeclareATask(t *testing.T) {
	for _, role := range roles {
		d := &fakeDaemon{}
		text, isErr := call(t, newServer(role, d), 1, "declare_task", map[string]any{"task": "matmul"})
		if (role == core.RoleLead) == isErr || (role != core.RoleLead && len(d.sent) != 0) {
			t.Errorf("%s: error=%v sent=%d %q", role, isErr, len(d.sent), text)
		}
	}
}

// The tool's rule and the command's are one: the proto says it twice, so a test holds them equal.
func TestTheToolsTaskNameRuleIsTheCommandsRule(t *testing.T) {
	pattern := func(md protoreflect.MessageDescriptor) string {
		return rulesOf(md.Fields().ByName("task")).GetString().GetPattern()
	}
	tool, command := pattern((&pb.DeclareTaskInput{}).ProtoReflect().Descriptor()), pattern((&pb.DeclareTask{}).ProtoReflect().Descriptor())
	if tool != command || tool != taskNamePattern {
		t.Fatalf("tool %q, command %q, test %q", tool, command, taskNamePattern)
	}
}

// What the orchestrator says back is worded for an agent that called the tool: a call after setup
// is told the task is fixed, and it is not told to leave the call to an operator.
func TestDeclareTaskAfterSetupIsToldTheTaskIsFixed(t *testing.T) {
	d := &fakeDaemon{receipt: &pb.Receipt{Refusal: pb.Refusal_REFUSAL_NOT_IN_SETUP}}
	text, isErr := call(t, newServer(core.RoleLead, d), 1, "declare_task", map[string]any{"task": "matmul"})
	if !isErr || !strings.Contains(text, "task is fixed") || strings.Contains(text, "operator completes") {
		t.Fatalf("%q", text)
	}
}
