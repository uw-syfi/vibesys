package execution

import (
	"context"
	"testing"
	"time"
)

func TestOSRunnerMergesSuiteEnvironment(t *testing.T) {
	t.Setenv("REPOCTL_TEST_OVERRIDE", "inherited")
	t.Setenv("REPOCTL_TEST_KEEP", "retained")
	suite := Suite{
		Job: "example", Directory: ".", TimeoutSeconds: 5,
		Commands: [][]string{{"sh", "-c", "test \"$REPOCTL_TEST_OVERRIDE\" = suite && test \"$REPOCTL_TEST_KEEP\" = retained"}},
		Env:      map[string]string{"REPOCTL_TEST_OVERRIDE": "suite"},
	}
	checks, err := Commands(suite)
	if err != nil {
		t.Fatal(err)
	}
	if len(checks) != 1 || checks[0].Timeout != 5*time.Second {
		t.Fatalf("planned checks = %+v", checks)
	}
	if err := (OSRunner{}).Run(context.Background(), t.TempDir(), checks[0]); err != nil {
		t.Fatal(err)
	}
}
