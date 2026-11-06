package execution

import (
	"context"
	"testing"
	"time"
)

func TestEnvironmentValuesNameTheRepositoryRoot(t *testing.T) {
	root := t.TempDir()
	check := Check{
		Label: "example", Directory: ".", Timeout: 5 * time.Second,
		Args: []string{"sh", "-c", "test \"$REPOCTL_TEST_PLACE\" = \"" + root + "/out\""},
		Env:  map[string]string{"REPOCTL_TEST_PLACE": RepoRootToken + "/out"},
	}
	if err := (OSRunner{}).Run(context.Background(), root, check); err != nil {
		t.Fatal(err)
	}
}

func TestOSRunnerMergesSuiteEnvironment(t *testing.T) {
	t.Setenv("REPOCTL_TEST_OVERRIDE", "inherited")
	t.Setenv("REPOCTL_TEST_KEEP", "retained")
	suite := Suite{
		Name: "example", Directory: ".", TimeoutSeconds: 5,
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
