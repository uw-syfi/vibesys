package envcodec_test

import (
	"testing"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// A job built against the last release of the evaluator package reads these variables by name: the
// messages keep each of them, so such a job runs unchanged. The release also reads the task's
// manifest itself, which the task directory still holds.
func TestTheNamesTheLastReleaseReadsAreStillWritten(t *testing.T) {
	declared := map[string]bool{}
	for _, m := range []proto.Message{&cliv1.JobEnvironment{}, &cliv1.KubernetesEnvironment{}} {
		names, err := envcodec.Names(m)
		if err != nil {
			t.Fatal(err)
		}
		for _, name := range names {
			declared[name] = true
		}
	}
	for _, name := range []string{"VIBESYS_ARTIFACTS_DIR", "VIBESYS_SERVICE_URL", "VIBESYS_MOUNTS", "VIBESYS_CACHE_DIR", "KUBECONFIG", "VIBESYS_K8S_REGISTRY", "VIBESYS_K8S_BUILDER"} {
		if !declared[name] {
			t.Errorf("%s is no longer written", name)
		}
	}
}
