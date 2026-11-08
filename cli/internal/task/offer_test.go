package task_test

import (
	"math/rand/v2"
	"os"
	"slices"
	"strings"
	"testing"

	"buf.build/go/protovalidate"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// What a client offers is what a card says. A card that carries no Unsupported says a job of this
// build can run the task, so the loader every job reads a task through must accept it: the class of
// bug where a host offers a task its own job tool then refuses ("[evaluator] tasks are not supported
// by this build", in the middle of a run) is closed here, over every task the repository ships.
//
// Both directions are held. A card that offers a task Read refuses is the bug. A card that refuses a
// task Read accepts would hide a runnable example, and a card of a task Read refuses says the
// loader's own words, so the client has one reason to show and the card has no list of its own.
func TestEveryExampleTaskIsOfferedExactlyWhenTheLoaderReadsIt(t *testing.T) {
	var offered, refused int
	reasons := map[string]int{}
	defer func() { t.Logf("%d example tasks offered, %d refused, by reason: %v", offered, refused, reasons) }()
	for _, e := range exampleTasks(t) {
		fsys := os.DirFS(e[0])
		card, err := task.Card(fsys, e[1])
		if err != nil {
			t.Errorf("%s task %s: no card: %v", e[0], e[1], err)
			continue
		}
		if err := protovalidate.Validate(card); err != nil {
			t.Errorf("%s task %s: card is invalid: %v", e[0], e[1], err)
		}
		_, readErr := task.Read(fsys, e[1])
		switch {
		case readErr == nil && card.GetUnsupported() != "":
			t.Errorf("%s task %s: the card says %q of a task Read accepts", e[0], e[1], card.GetUnsupported())
		case readErr != nil && card.GetUnsupported() == "":
			t.Errorf("%s task %s: the card offers a task Read refuses: %v", e[0], e[1], readErr)
		case readErr != nil:
			refused++
			reasons[strings.SplitN(card.GetUnsupported(), ";", 2)[0]]++
			// An example the build cannot run is refused as unsupported, never as invalid, and the
			// card quotes the refusal.
			if f := core.AsFailure(readErr); f.Kind != core.FailureUnavailable {
				t.Errorf("%s task %s: Read refuses it as %v, not as unsupported: %v", e[0], e[1], f.Kind, readErr)
			} else if !strings.Contains(f.Message, card.GetUnsupported()) {
				t.Errorf("%s task %s: the card says %q, Read says %q", e[0], e[1], card.GetUnsupported(), f.Message)
			}
		default:
			offered++
		}
	}
	// A repository whose examples were all offered, or all refused, would pass this test for the
	// wrong reason.
	if offered == 0 || refused == 0 {
		t.Fatalf("%d example tasks offered and %d refused: the table has one side", offered, refused)
	}
}

// refusals are the ways a manifest of the single-image format is refused, each applied to a task Read
// accepts: what the loader does not run, and what it finds invalid.
var refusals = map[string]func(string) string{
	"an evaluator":        func(m string) string { return m + "\n[evaluator]\nname = \"queue\"\n" },
	"a modal environment": func(m string) string { return m + "\n[environment.modal]\ngpu = \"A100\"\n" },
	"no accuracy":         func(m string) string { return strings.Replace(m, "[accuracy]", "[unrelated]", 1) },
	"an unknown key":      func(m string) string { return m + "\nsurprise = 1\n" },
	"several backends":    func(m string) string { return strings.Replace(m, `backends = ["cpu"]`, `backends = ["cpu", "gpu"]`, 1) },
	"an evaluator entrypoint": func(m string) string {
		return strings.Replace(m, "[accuracy]\n", "[accuracy]\nentrypoint = \"check\"\n", 1)
	},
	"an unknown placeholder": func(m string) string { return strings.Replace(m, "--strict", "${NOWHERE}", 1) },
	"a timeout past the limit": func(m string) string {
		return strings.Replace(m, "timeout_seconds = 600", "timeout_seconds = 604801", 1)
	},
	"no result contract": func(m string) string {
		return strings.Replace(m, "[benchmark.result]\njson_argument = \"--output-json\"\nmetric = \"ops_per_sec\"\n", "", 1)
	},
	"a measurement without a check": func(m string) string {
		return strings.Replace(m, "[benchmark]\n", "[benchmark]\nrequires_accuracy = false\n", 1)
	},
}

// Whatever combination of refusals a manifest has, its card is empty of Unsupported exactly when Parse,
// the function Read reads a task through, accepts the files, and otherwise it says Parse's own refusal
// (every refusal of a combination is one Parse could have named first). So a refusal added to the loader
// needs no edit to the card, and no card can offer what a job refuses.
func TestACardSaysUnsupportedExactlyWhenTheLoaderRefusesTheFiles(t *testing.T) {
	names := make([]string, 0, len(refusals))
	for name := range refusals {
		names = append(names, name)
	}
	slices.Sort(names)
	r := rand.New(rand.NewPCG(11, 13))
	for i := 0; i < 300; i++ {
		manifest := jsonManifest
		var applied []string
		for _, name := range names {
			if r.IntN(4) == 0 {
				manifest = refusals[name](manifest)
				applied = append(applied, name)
			}
		}
		card, err := task.Card(oneTask(manifest, objectives, ""), "t")
		if err != nil {
			// The card itself could not be made (an unreadable table): nothing is offered.
			if core.AsFailure(err).Kind != core.FailureInvalidInput {
				t.Fatalf("%v: %v", applied, err)
			}
			continue
		}
		_, parseErr := task.Parse("t", []byte(manifest), []byte(objectives))
		if (parseErr == nil) != (card.GetUnsupported() == "") {
			t.Fatalf("%v: Parse says %v, the card says %q", applied, parseErr, card.GetUnsupported())
		}
		if parseErr != nil && !strings.HasPrefix(core.AsFailure(parseErr).Message, card.GetUnsupported()) {
			t.Fatalf("%v: Parse says %q, the card says %q", applied, core.AsFailure(parseErr).Message, card.GetUnsupported())
		}
		if len(applied) == 0 && card.GetUnsupported() != "" {
			t.Fatalf("a task Read accepts is unsupported: %q", card.GetUnsupported())
		}
		if len(applied) > 0 && card.GetUnsupported() == "" {
			t.Fatalf("%v: a refused task is offered", applied)
		}
		if err := protovalidate.Validate(card); err != nil {
			t.Fatalf("%v: %v", applied, err)
		}
	}
}

// The reason fits the card's bound and its text, however long and whatever bytes a refusal quotes, so
// a card of a task with a very long key still meets its rules and is still shown.
func TestTheUnsupportedReasonAlwaysFitsTheCard(t *testing.T) {
	r := rand.New(rand.NewPCG(17, 19))
	for i := 0; i < 100; i++ {
		key := strings.Repeat("ké", r.IntN(2000))
		card, err := task.Card(oneTask("\"x"+key+"\" = 1\n"+jsonManifest, objectives, ""), "t")
		if err != nil {
			t.Fatal(err)
		}
		if card.GetUnsupported() == "" {
			t.Fatalf("an unknown key of %d bytes is accepted", len(key))
		}
		if err := protovalidate.Validate(card); err != nil {
			t.Fatalf("key of %d bytes: %v", len(key), err)
		}
	}
}
