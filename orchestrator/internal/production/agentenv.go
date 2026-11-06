package production

import (
	"fmt"
	"maps"
	"strings"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// agentEnvironment is what the session's processes get over their login environment. The run's
// identity is an AgentContext and the path the skills run is a SkillEnvironment, each written by the
// environment codec, so the names are the env_name of their fields (proto/vibesys/cli/v1/env.proto)
// and nothing here spells one. An empty value unsets its name, so the program resolves it as this
// process would. The job tool's places (XDG_RUNTIME_DIR, XDG_STATE_HOME and HOME) are the one part
// not yet a message; they stay until the job tool's endpoint replaces them, and HOME is kept when
// empty because the agent's own program needs it. The token of the session is never here: it goes to
// the tool server alone, see toolServerEnvironment.
func agentEnvironment(start daemon.SessionStart) (*pb.AgentEnvironment, error) {
	l, where := start.Launch, start.Location
	out := &pb.AgentEnvironment{Set: map[string]string{}}
	for _, m := range []proto.Message{
		&cliv1.AgentContext{
			Repository: start.Repository, Task: start.Task, RunId: string(l.Run), Agent: string(l.Session.Agent),
			Harness: strings.ReplaceAll(string(l.Spec.Harness), "_", "-"),
		},
		&cliv1.SkillEnvironment{Cli: where.CLI},
	} {
		encoded, err := envcodec.Encode(m)
		if err != nil {
			return nil, fmt.Errorf("the agent's environment: %w", err)
		}
		maps.Copy(out.Set, encoded.Set)
		out.Unset = append(out.Unset, encoded.Unset...)
	}
	for _, name := range []struct{ name, value string }{
		{"XDG_RUNTIME_DIR", where.RuntimeDir},
		{"XDG_STATE_HOME", where.StateHome},
		{"HOME", where.Home},
	} {
		switch {
		case name.value != "":
			out.Set[name.name] = name.value
		case name.name != "HOME":
			out.Unset = append(out.Unset, name.name)
		}
	}
	return out, nil
}

// toolServerEnvironment is the environment of the session's tool server: its credentials, which it
// alone gets, over the identity the agent's own processes get.
func toolServerEnvironment(credentials *pb.ToolServerCredentials, agent *pb.AgentEnvironment) (map[string]string, error) {
	encoded, err := envcodec.Encode(credentials)
	if err != nil {
		return nil, fmt.Errorf("the tool server's credentials: %w", err)
	}
	env := make(map[string]string, len(agent.GetSet())+len(encoded.Set))
	maps.Copy(env, agent.GetSet())
	maps.Copy(env, encoded.Set)
	return env, nil
}
