package main

import (
	"strings"
	"testing"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/encoding/protojson"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// cardCLIManifest is a task this build runs: a check, a benchmark and the facts of a card. Its card
// carries no word of what it cannot run.
const cardCLIManifest = `version = "0.2"
[accuracy]
command = ["true"]
[benchmark]
command = ["true"]
result_protocol = 2
[catalogue]
domain = "llm-serving"
[catalogue.hardware]
accelerator = %q
count = 8
vendor = "nvidia"
[catalogue.model]
name = "GLM 5.2"
maker = "zhipu"
`

func cardCLITree(accelerator string) task.Tree {
	return task.Tree{
		".vibesys/tasks/glm/" + task.ManifestName:  {Data: []byte(strings.Replace(cardCLIManifest, "%q", `"`+accelerator+`"`, 1))},
		".vibesys/tasks/glm/" + task.ObjectiveName: {Data: []byte("[[objective]]\nname = \"tok_s\"\ndirection = \"max\"\nunit = \"tok/s\"\n")},
		".vibesys/tasks/glm/OBJECTIVE.md":          {Data: []byte("# GLM serving\n")},
	}
}

func (c *cli) putCard(accelerator string) {
	tree := cardCLITree(accelerator)
	c.world.CardFiles.PutCommit("/repo", "HEAD", tree)
	c.world.CardFiles.PutCommit("/repo", "v1", tree)
	c.world.CardFiles.PutWorkingTree("/repo", tree)
	c.world.CardFiles.PutFolder("/examples/glm", tree)
}

// The card prints the same from every source, in words and as the contract's message, and the JSON is
// a card that meets its rules.
func TestTaskCardPrintsWhatAClientShows(t *testing.T) {
	c := newCLI(t)
	c.putCard("NVIDIA B200")
	for name, args := range map[string][]string{
		"head":         {"task", "card", "glm"},
		"revision":     {"task", "card", "glm", "--revision", "v1"},
		"working tree": {"task", "card", "glm", "--working-tree"},
		"folder":       {"task", "card", "glm", "--folder", "/examples/glm"},
		"task flag":    {"task", "card", "--task", "glm"},
	} {
		r := c.run(args...)
		want := "task\tglm\nheading\tGLM serving\ndomain\tllm-serving\nhardware\t8 x NVIDIA B200\nmodel\tGLM 5.2 (zhipu)\nobjective\ttok_s\tmax\ttok/s\n"
		if r.code != exitOK || r.stdout != want {
			t.Errorf("%s: exit %d, %q (%q), want %q", name, r.code, r.stdout, r.stderr, want)
		}
		j := c.run(append(args, "--json")...)
		var card cliv1.TaskCard
		if err := protojson.Unmarshal([]byte(j.stdout), &card); err != nil || j.code != exitOK {
			t.Fatalf("%s: --json %q: %v", name, j.stdout, err)
		}
		if err := protovalidate.Validate(&card); err != nil {
			t.Errorf("%s: %v", name, err)
		}
		if card.GetHardware().GetModel() != cliv1.AcceleratorModel_ACCELERATOR_MODEL_B200 || card.GetHardware().GetCount() != 8 || card.GetModelName() != "GLM 5.2" {
			t.Errorf("%s: card %v", name, &card)
		}
	}
}

// A task this build cannot run still has a card, and the card says why in words and in the contract's
// field, from every source: a client never offers a task that no job of this build could run.
func TestTaskCardSaysWhyABuildCannotRunTheTask(t *testing.T) {
	c := newCLI(t)
	tree := cardCLITree("NVIDIA B200")
	manifest := tree[".vibesys/tasks/glm/"+task.ManifestName]
	// The single-image format is the one whose reader knows an [evaluator] and does not run it.
	older := strings.Replace(string(manifest.Data), `version = "0.2"`, `version = "0.1"`, 1)
	tree[".vibesys/tasks/glm/"+task.ManifestName] = task.File{Data: []byte(older + "[evaluator]\nname = \"queue\"\n")}
	c.world.CardFiles.PutCommit("/repo", "HEAD", tree)
	c.world.CardFiles.PutFolder("/examples/glm", tree)
	for name, args := range map[string][]string{
		"head":   {"task", "card", "glm"},
		"folder": {"task", "card", "glm", "--folder", "/examples/glm"},
	} {
		r := c.run(args...)
		if r.code != exitOK || !strings.Contains(r.stdout, "hardware\t8 x NVIDIA B200\n") || !strings.Contains(r.stdout, "unsupported\t"+task.ManifestName+": [evaluator] tasks are not supported by this build") {
			t.Errorf("%s: exit %d, %q (%q)", name, r.code, r.stdout, r.stderr)
		}
		j := c.run(append(args, "--json")...)
		var card cliv1.TaskCard
		if err := protojson.Unmarshal([]byte(j.stdout), &card); err != nil || j.code != exitOK {
			t.Fatalf("%s: --json %q: %v", name, j.stdout, err)
		}
		if err := protovalidate.Validate(&card); err != nil {
			t.Errorf("%s: %v", name, err)
		}
		if !strings.Contains(card.GetUnsupported(), "[evaluator]") || card.GetHardware().GetCount() != 8 {
			t.Errorf("%s: card %v", name, &card)
		}
	}
}

// A task with no card says why and exits 2, naming the file and the key, in words and as JSON.
func TestTaskCardRefusesATaskItCannotRead(t *testing.T) {
	c := newCLI(t)
	c.putCard("NVIDIA H200")
	for _, args := range [][]string{{"task", "card", "glm"}, {"task", "card", "glm", "--json"}} {
		r := c.run(args...)
		if r.code != exitInvalidInput || r.stdout != "" || !strings.Contains(r.stderr, task.ManifestName) ||
			!strings.Contains(r.stderr, "accelerator") || !strings.Contains(r.stderr, "NVIDIA H200") {
			t.Errorf("%v: exit %d, stdout %q, stderr %q", args, r.code, r.stdout, r.stderr)
		}
	}
	c.putCard("NVIDIA B200")
	for name, args := range map[string][]string{
		"missing task":    {"task", "card", "absent"},
		"missing folder":  {"task", "card", "glm", "--folder", "/nowhere"},
		"missing version": {"task", "card", "glm", "--revision", "v9"},
	} {
		if r := c.run(args...); r.code != exitInvalidInput || r.stdout != "" {
			t.Errorf("%s: exit %d, %q, %q; want 2", name, r.code, r.stdout, r.stderr)
		}
	}
}

func TestTaskCardRefusesBadUsage(t *testing.T) {
	c := newCLI(t)
	for name, args := range map[string][]string{
		"no subcommand":   {"task"},
		"other word":      {"task", "show"},
		"no task":         {"task", "card"},
		"two tasks":       {"task", "card", "a", "b"},
		"two sources":     {"task", "card", "a", "--working-tree", "--folder", "/x"},
		"revision + tree": {"task", "card", "a", "--revision", "v1", "--working-tree"},
	} {
		if r := c.run(args...); r.code != exitInvalidInput || r.stdout != "" || r.stderr == "" {
			t.Errorf("%s: exit %d, %q, %q; want 2 with a message", name, r.code, r.stdout, r.stderr)
		}
	}
}
