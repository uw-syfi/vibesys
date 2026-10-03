package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeGitHubEvent(t *testing.T, name, payload string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "event.json")
	if err := os.WriteFile(path, []byte(payload), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("GITHUB_EVENT_NAME", name)
	t.Setenv("GITHUB_EVENT_PATH", path)
}

func TestGitHubEventRevisions(t *testing.T) {
	base := strings.Repeat("a", 40)
	head := strings.Repeat("b", 40)
	for _, tc := range []struct {
		name, event, payload, wantBase string
	}{
		{"pull request", "pull_request", `{"pull_request":{"base":{"sha":"` + base + `"},"head":{"sha":"` + head + `"}}}`, base},
		{"merge queue", "merge_group", `{"merge_group":{"base_sha":"` + base + `","head_sha":"` + head + `"}}`, base},
		{"push", "push", `{"before":"` + base + `","after":"` + head + `"}`, base},
		{"initial push", "push", `{"before":"` + strings.Repeat("0", 40) + `","after":"` + head + `"}`, strings.Repeat("0", 40)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			writeGitHubEvent(t, tc.event, tc.payload)
			gotBase, gotHead, gotEvent, err := gitHubEventRevisions()
			if err != nil || gotBase != tc.wantBase || gotHead != head || gotEvent != tc.event {
				t.Fatalf("revisions = %q, %q, %q, err = %v", gotBase, gotHead, gotEvent, err)
			}
		})
	}
}

func TestGitHubEventRevisionsRejectsInvalidPayload(t *testing.T) {
	sha := strings.Repeat("a", 40)
	for _, tc := range []struct {
		name, event, payload, want string
	}{
		{"unsupported", "workflow_dispatch", `{}`, "unsupported GitHub event"},
		{"malformed JSON", "push", `{`, "malformed GitHub event"},
		{"missing pull request", "pull_request", `{}`, "pull_request.base.sha"},
		{"missing merge group", "merge_group", `{}`, "merge_group.base_sha"},
		{"missing push head", "push", `{"before":"` + sha + `"}`, "valid base and head SHAs"},
		{"unsafe SHA", "push", `{"before":"--help","after":"` + sha + `"}`, "valid base and head SHAs"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			writeGitHubEvent(t, tc.event, tc.payload)
			_, _, _, err := gitHubEventRevisions()
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error = %v, want %q", err, tc.want)
			}
		})
	}
	t.Run("missing environment", func(t *testing.T) {
		t.Setenv("GITHUB_EVENT_NAME", "")
		t.Setenv("GITHUB_EVENT_PATH", "")
		if _, _, _, err := gitHubEventRevisions(); err == nil || !strings.Contains(err.Error(), "requires GITHUB_EVENT_NAME") {
			t.Fatalf("error = %v", err)
		}
	})
	t.Run("missing file", func(t *testing.T) {
		t.Setenv("GITHUB_EVENT_NAME", "push")
		t.Setenv("GITHUB_EVENT_PATH", filepath.Join(t.TempDir(), "missing.json"))
		if _, _, _, err := gitHubEventRevisions(); err == nil || !strings.Contains(err.Error(), "read GitHub event") {
			t.Fatalf("error = %v", err)
		}
	})
}

func TestPlanGitHubEventRejectsExplicitRevisions(t *testing.T) {
	config := filepath.Join(fixtureRepo(t), "repoctl.toml")
	for _, flag := range []string{"--base=main", "--head=HEAD", "--event=pull_request"} {
		t.Run(flag, func(t *testing.T) {
			err := cli([]string{"--config", config, "plan", "--github-event", flag})
			if err == nil || !strings.Contains(err.Error(), "cannot be combined") {
				t.Fatalf("error = %v", err)
			}
		})
	}
}

func TestPlanGitHubEventUsesPayloadRevisions(t *testing.T) {
	root := fixtureRepo(t)
	base := commitFixture(t, root, "base")
	writeFixtureFile(t, root, "python/service/handler.py", "changed\n")
	head := commitFixture(t, root, "change service")
	writeGitHubEvent(t, "push", `{"before":"`+base+`","after":"`+head+`"}`)
	output := filepath.Join(t.TempDir(), "github-output")
	if err := cli([]string{"--config", filepath.Join(root, "repoctl.toml"), "plan", "--github-event", "--github-output", output}); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(output)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), "unit=true\n") {
		t.Fatalf("GitHub outputs did not select unit: %s", raw)
	}
}
