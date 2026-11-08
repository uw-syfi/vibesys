package clientcli_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli"
)

// --repositories-directory records the host's default repositories directory beside the other fields,
// and given empty puts the default back.
func TestHostConfigureRecordsTheRepositoriesDirectory(t *testing.T) {
	configs := &fakeConfigs{}
	env := hostWorld(&fakeHost{identity: identity(0)}, nil)
	env.Configs = configs
	if code, _, errb := runHost(env, "host", "configure", "--job-tool", "/x/tool", "--repositories-directory", "~/work/repos"); code != clientcli.ExitOK {
		t.Fatalf("exit %d: %s", code, errb)
	}
	if configs.config.GetRepositoriesDirectory() != "~/work/repos" || configs.config.GetJobTool() != "/x/tool" {
		t.Fatalf("recorded %v", configs.config)
	}
	if code, _, _ := runHost(env, "host", "configure", "--repositories-directory", ""); code != clientcli.ExitOK || configs.config.GetRepositoriesDirectory() != "" {
		t.Fatalf("empty: exit %d, %v", code, configs.config)
	}
}
