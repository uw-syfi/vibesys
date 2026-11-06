package production

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// The credential the host gives a session's tool server is named as one in the request, so the
// holder can keep it out of every log; the names that only say where the host is are not.
func TestTheOpenRequestNamesTheCredentialAsTheOnlyOne(t *testing.T) {
	start := sessionStart("agent-1", core.RoleLead, "/repo")
	request, err := newHolderWorld().sidecars("/sessions").openRequest(start, Plugin{Marketplace: "/skills", Name: "vibesys"})
	if err != nil {
		t.Fatal(err)
	}
	token := envcodec.NameOf(&pb.ToolServerCredentials{}, "token")
	tools := request.GetTools()
	if len(tools) != 1 || len(tools[0].GetSecretEnv()) != 1 || tools[0].GetSecretEnv()[0] != token {
		t.Fatalf("the tool server's credentials are %v, want only %s", tools, token)
	}
	if _, in := tools[0].GetEnv()[token]; !in {
		t.Fatalf("a name in secret_env must be in env: %v", tools[0].GetEnv())
	}
}
