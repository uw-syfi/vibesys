package task

import (
	"io/fs"
	"path"
	"strings"
	"unicode/utf8"

	"github.com/BurntSushi/toml"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// DescriptionName is the file a task's heading is read from.
const DescriptionName = "OBJECTIVE.md"

// maxHeadingBytes is the card's bound on a heading (TaskCard.heading).
const maxHeadingBytes = 200

// maxUnsupportedBytes is the card's bound on the reason a task cannot run (TaskCard.unsupported).
const maxUnsupportedBytes = 2048

// Card reads the task called name from a repository whose root is fsys and returns what a client
// shows of it: the facts of [catalogue], the objectives, and the heading of OBJECTIVE.md.
//
// It reads those three files and no more, so a task that cannot run on this machine (an evaluator,
// a modal environment) still has a card, and the card says so in Unsupported: the refusal Parse
// makes of the same files, which is the one Read, and so every job, makes. A task without
// [catalogue] has the domain unspecified and no hardware. A file that is invalid is a
// core.FailureInvalidInput naming the file and the key; a task that is not there is
// core.FailureNotFound. The card it returns meets the rules of its message.
func Card(fsys fs.FS, name string) (*cliv1.TaskCard, error) {
	if err := ValidName(name); err != nil {
		return nil, err
	}
	dir := path.Join(TasksDir, name)
	info, err := fs.Stat(fsys, dir)
	if err != nil || !info.IsDir() {
		return nil, &core.Failure{Kind: core.FailureNotFound, Message: "task " + quote(name) + ": there is no directory " + dir}
	}
	manifest, err := fs.ReadFile(fsys, path.Join(dir, ManifestName))
	if err != nil {
		return nil, &core.Failure{Kind: core.FailureNotFound, Message: "task " + quote(name) + ": no " + ManifestName + " in " + dir}
	}
	objectives, err := readOptional(fsys, path.Join(dir, ObjectiveName))
	if err != nil {
		return nil, err
	}
	description, err := readOptional(fsys, path.Join(dir, DescriptionName))
	if err != nil {
		return nil, err
	}
	card, err := CardOf(name, manifest, objectives, description)
	if err != nil {
		f := core.AsFailure(err)
		return nil, &core.Failure{Kind: f.Kind, Message: "task " + quote(name) + " (" + dir + "): " + f.Message}
	}
	return card, nil
}

// CardOf builds the card from the contents of the three files; objectivesTOML and description are
// nil when the task has none.
func CardOf(name string, manifestTOML, objectivesTOML, description []byte) (*cliv1.TaskCard, error) {
	var table map[string]any
	if _, err := toml.Decode(string(manifestTOML), &table); err != nil {
		return nil, invalid("%s: %v", ManifestName, err)
	}
	o, err := ParseObjectives(objectivesTOML)
	if err != nil {
		return nil, err
	}
	card := &cliv1.TaskCard{Task: name, Heading: Heading(description), Unsupported: unsupportedReason(name, manifestTOML, objectivesTOML)}
	for _, obj := range o.GetObjective() {
		card.Objectives = append(card.Objectives, &cliv1.CardObjective{Name: obj.GetName(), Direction: obj.GetDirection(), Unit: obj.GetUnit()})
	}
	if raw, ok := table["catalogue"]; ok {
		t, isTable := raw.(map[string]any)
		if !isTable {
			return nil, invalid("%s: catalogue: must be a table", ManifestName)
		}
		c, err := Catalogue(t)
		if err != nil {
			return nil, err
		}
		if err := fillFromCatalogue(card, c); err != nil {
			return nil, err
		}
	}
	if err := validate("the card of task "+quote(name), card); err != nil {
		return nil, err
	}
	return card, nil
}

// unsupportedReason says why this build's job tool cannot run a task of these files, and is empty when
// it can. It is not a second list of what the tool supports: it asks Parse, the function Read reads
// a task through, so the card refuses exactly what a job refuses and a new refusal of the loader
// reaches every card, and so every client's offer, with no other edit. It holds the manifest and
// the objectives only, the files a card reads; what Read finds in the rest of the task's directory
// (a Dockerfile a [service] needs, a link that leaves the directory) is for the first job.
func unsupportedReason(name string, manifestTOML, objectivesTOML []byte) string {
	_, err := Parse(name, manifestTOML, objectivesTOML)
	if err == nil {
		return ""
	}
	return clip(strings.ToValidUTF8(core.AsFailure(err).Message, ""), maxUnsupportedBytes)
}

func fillFromCatalogue(card *cliv1.TaskCard, c *cliv1.TaskCatalogueV02) error {
	card.Domain = c.GetDomain()
	need, err := cardHardware(c.GetHardware())
	if err != nil {
		return err
	}
	card.Hardware, card.HardwareName = need, c.GetHardware().GetAccelerator()
	if m := c.GetModel(); m != nil {
		card.ModelName, card.ModelMaker = m.GetName(), m.GetMaker()
	}
	if w := c.GetWorkload(); w != nil {
		card.WorkloadName, card.WorkloadDescription = w.GetName(), w.GetDescription()
	}
	return nil
}

// Heading is the text of the first Markdown heading of a file, outside any fenced code block,
// without its marks, cut at a character boundary to fit the card. It is empty for a file with none.
func Heading(markdown []byte) string {
	fence := ""
	for _, line := range strings.Split(strings.ReplaceAll(string(markdown), "\r\n", "\n"), "\n") {
		trimmed := strings.TrimLeft(line, " ")
		if len(line)-len(trimmed) > 3 {
			continue // indented code
		}
		if mark := fenceOf(trimmed); mark != "" {
			switch {
			case fence == "":
				fence = mark
			case mark[0] == fence[0] && len(mark) >= len(fence):
				fence = ""
			}
			continue
		}
		if fence != "" || !strings.HasPrefix(trimmed, "#") {
			continue
		}
		hashes := len(trimmed) - len(strings.TrimLeft(trimmed, "#"))
		rest := trimmed[hashes:]
		if hashes > 6 || (rest != "" && rest[0] != ' ' && rest[0] != '\t') {
			continue
		}
		text := strings.TrimSpace(strings.TrimRight(strings.TrimSpace(rest), "#"))
		if text == "" {
			continue
		}
		return clip(text, maxHeadingBytes)
	}
	return ""
}

// fenceOf returns the run of backticks or tildes (three or more) that a line opens or closes a fence
// with, and the empty string for any other line.
func fenceOf(line string) string {
	for _, c := range []string{"`", "~"} {
		n := len(line) - len(strings.TrimLeft(line, c))
		if n >= 3 {
			return line[:n]
		}
	}
	return ""
}

// clip cuts s to at most limit bytes without splitting a character.
func clip(s string, limit int) string {
	if len(s) <= limit {
		return s
	}
	for limit > 0 && !utf8.RuneStart(s[limit]) {
		limit--
	}
	return strings.TrimRight(s[:limit], " ")
}

func quote(s string) string { return "\"" + s + "\"" }
