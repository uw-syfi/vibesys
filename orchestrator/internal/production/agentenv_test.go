package production

import (
	"context"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

func someWord(r *rand.Rand, max int) string {
	letters := "abcxyz019-_"
	out := make([]byte, r.IntN(max+1))
	for i := range out {
		out[i] = letters[r.IntN(len(letters))]
	}
	return string(out)
}

// maybePath is an absolute path, or empty.
func maybePath(r *rand.Rand) string {
	if r.IntN(3) == 0 {
		return ""
	}
	return "/" + someWord(r, 20)
}

func names(t *testing.T, m proto.Message) []string {
	t.Helper()
	out, err := envcodec.Names(m)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// Whatever a start holds, the session is opened with an agent environment that the codec reads back
// as the run's identity: each name is the env_name of a field, an empty value is unset and never set
// to the empty string, and no place of the operator's is touched. The agent's environment
// never holds the session's token, which only its tool server's does, and the tool server's
// credentials read back as exactly what the daemon gave.
func TestTheSessionIsOpenedWithTheEnvironmentTheMessagesDeclare(t *testing.T) {
	r := rand.New(rand.NewPCG(13, 17))
	identity := slices.Concat(names(t, &cliv1.AgentContext{}), names(t, &cliv1.SkillEnvironment{}), names(t, &cliv1.JobToolPlace{}))
	for i := range 300 {
		w := newHolderWorld()
		start := sessionStart("agent-1", core.RoleLead, "/"+someWord(r, 12))
		start.Task = someWord(r, 12)
		start.Launch.Run = core.RunID(someWord(r, 12))
		start.Launch.Spec.Harness = []core.Harness{core.ClaudeCode, core.Codex}[r.IntN(2)]
		start.JobTool = core.JobToolEndpoint{Executable: maybePath(r), RuntimeDirectory: maybePath(r)}
		start.Tools.Token = "secret-" + someWord(r, 30)
		if r.IntN(2) == 0 {
			start.Repository = "/" + someWord(r, 12)
		}
		if _, err := w.sidecars("/sessions").Launch(context.Background(), start); err != nil {
			t.Fatalf("round %d: %v", i, err)
		}
		if len(w.holders) != 1 {
			t.Fatalf("round %d: %d holders", i, len(w.holders))
		}
		var opened *pb.OpenSessionRequest
		for _, h := range w.holders {
			opened = h.opened
		}
		environment := opened.GetEnvironment()

		for name, value := range environment.GetSet() {
			if value == "" {
				t.Errorf("round %d: %s is set to the empty string", i, name)
			}
			if !slices.Contains(identity, name) {
				t.Errorf("round %d: %s is set and no message declares it", i, name)
			}
			if _, unset := slices.BinarySearch(slices.Sorted(slices.Values(environment.GetUnset())), name); unset {
				t.Errorf("round %d: %s is both set and unset", i, name)
			}
		}
		for _, name := range identity {
			_, set := environment.GetSet()[name]
			if set == slices.Contains(environment.GetUnset(), name) {
				t.Errorf("round %d: %s must be exactly one of set and unset", i, name)
			}
		}

		var context cliv1.AgentContext
		if err := envcodec.Decode(func(name string) string { return environment.GetSet()[name] }, &context); err != nil {
			t.Fatalf("round %d: %v", i, err)
		}
		wantContext := &cliv1.AgentContext{
			Repository: start.Repository, Task: start.Task, RunId: string(start.Launch.Run), Agent: "agent-1",
			Harness: strings.ReplaceAll(string(start.Launch.Spec.Harness), "_", "-"),
		}
		if !proto.Equal(&context, wantContext) {
			t.Fatalf("round %d: the agent reads %v, want %v", i, &context, wantContext)
		}
		if got := environment.GetSet()[envcodec.NameOf(&cliv1.SkillEnvironment{}, "cli")]; got != start.JobTool.Executable {
			t.Errorf("round %d: the skills' CLI is %q, want %q", i, got, start.JobTool.Executable)
		}
		if got := environment.GetSet()[envcodec.NameOf(&cliv1.JobToolPlace{}, "runtime_directory")]; got != start.JobTool.RuntimeDirectory {
			t.Errorf("round %d: the job tool's place is %q, want %q", i, got, start.JobTool.RuntimeDirectory)
		}

		token := envcodec.NameOf(&pb.ToolServerCredentials{}, "token")
		if _, ok := environment.GetSet()[token]; ok || slices.Contains(environment.GetUnset(), token) {
			t.Fatalf("round %d: the agent's environment names the session's token", i)
		}
		if len(opened.GetTools()) != 1 {
			t.Fatalf("round %d: %d tool servers", i, len(opened.GetTools()))
		}
		tool := opened.GetTools()[0]
		for _, argument := range tool.GetArgv() {
			if strings.Contains(argument, start.Tools.GetToken()) {
				t.Fatalf("round %d: the token is in the tool server's argument %q", i, argument)
			}
		}
		var credentials pb.ToolServerCredentials
		if err := envcodec.Decode(func(name string) string { return tool.GetEnv()[name] }, &credentials); err != nil {
			t.Fatalf("round %d: %v", i, err)
		}
		if !proto.Equal(&credentials, start.Tools) {
			t.Fatalf("round %d: the tool server reads %v, the daemon gave %v", i, &credentials, start.Tools)
		}
	}
}
