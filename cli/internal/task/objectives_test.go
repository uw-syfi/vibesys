package task_test

import (
	"fmt"
	"math"
	"math/rand/v2"
	"strconv"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// render writes a message as the objectives.toml an author would, the keys being its field names.
func render(o *cliv1.TaskObjectives) string {
	var b strings.Builder
	for _, obj := range o.GetObjective() {
		b.WriteString("[[objective]]\n")
		fmt.Fprintf(&b, "name = %s\n", strconv.Quote(obj.GetName()))
		dir := "max"
		if obj.GetDirection() == cliv1.TaskObjectiveDirection_TASK_OBJECTIVE_DIRECTION_MIN {
			dir = "min"
		}
		fmt.Fprintf(&b, "direction = %q\n", dir)
		if obj.RelativeNoise != nil {
			fmt.Fprintf(&b, "relative_noise = %s\n", strconv.FormatFloat(obj.GetRelativeNoise(), 'g', -1, 64))
		}
	}
	if o.GetPareto() != nil {
		fmt.Fprintf(&b, "[pareto]\nrelative_noise = %s\n", strconv.FormatFloat(o.GetPareto().GetRelativeNoise(), 'g', -1, 64))
	}
	return b.String()
}

// anyObjectives is a valid message: a few objectives with distinct names, and when there is a
// [pareto] section and more than one objective, each may declare its own noise floor.
func anyObjectives(r *rand.Rand) *cliv1.TaskObjectives {
	floor := func() float64 {
		return []float64{0, 0.02, 0.5, 0.999, r.Float64() * 0.999}[r.IntN(5)]
	}
	o := &cliv1.TaskObjectives{}
	for i := range r.IntN(5) {
		dir := cliv1.TaskObjectiveDirection_TASK_OBJECTIVE_DIRECTION_MAX
		if r.IntN(2) == 0 {
			dir = cliv1.TaskObjectiveDirection_TASK_OBJECTIVE_DIRECTION_MIN
		}
		o.Objective = append(o.Objective, &cliv1.TaskObjective{
			Name:      fmt.Sprintf("metric_%d_%c \"é\\", i, 'a'+rune(r.IntN(26))),
			Direction: dir,
		})
	}
	if r.IntN(2) == 0 {
		o.Pareto = &cliv1.TaskPareto{RelativeNoise: floor()}
		if len(o.Objective) > 1 {
			for _, obj := range o.Objective {
				if r.IntN(2) == 0 {
					obj.RelativeNoise = proto.Float64(floor())
				}
			}
		}
	}
	return o
}

func TestObjectivesRoundTripThroughTOML(t *testing.T) {
	r := rand.New(rand.NewPCG(7, 8))
	for range 2000 {
		o := anyObjectives(r)
		text := render(o)
		got, err := task.ParseObjectives([]byte(text))
		if err != nil {
			t.Fatalf("%v read from\n%s", err, text)
		}
		if !proto.Equal(got, o) {
			t.Fatalf("%v read from\n%s\nas %v", o, text, got)
		}
	}
}

func TestNoObjectivesFileIsNoObjectives(t *testing.T) {
	for _, data := range [][]byte{nil, {}, []byte("# nothing\n")} {
		got, err := task.ParseObjectives(data)
		if err != nil || len(got.GetObjective()) != 0 || got.GetPareto() != nil {
			t.Errorf("%q: %v, %v", data, got, err)
		}
	}
}

// A key the message has no field for is refused naming the key and the file, at every place a key
// can stand: the top level, an objective and [pareto].
func TestAnUnknownObjectivesKeyIsRefusedNamingItAndTheFile(t *testing.T) {
	r := rand.New(rand.NewPCG(9, 10))
	for range 500 {
		o := anyObjectives(r)
		if o.Pareto == nil {
			o.Pareto = &cliv1.TaskPareto{}
		}
		key := fmt.Sprintf("zz_%c%c%d", 'a'+rune(r.IntN(26)), 'a'+rune(r.IntN(26)), r.IntN(1000))
		var text, want string
		switch place := r.IntN(3); {
		case place == 0:
			text, want = key+" = 1\n"+render(o), key
		case place == 1 && len(o.Objective) > 0:
			text = strings.Replace(render(o), "[[objective]]\n", "[[objective]]\n"+key+" = 1\n", 1)
			want = "objective[0]." + key
		default:
			text = strings.Replace(render(o), "[pareto]\n", "[pareto]\n"+key+" = 1\n", 1)
			want = "pareto." + key
		}
		_, err := task.ParseObjectives([]byte(text))
		if err == nil {
			t.Fatalf("accepted\n%s", text)
		}
		if f := failureOf(t, err); f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, task.ObjectiveName) || !strings.Contains(f.Message, `"`+want+`"`) {
			t.Fatalf("%q: error %q does not name the file and %q", text, f.Message, want)
		}
	}
}

// Whatever is malformed is refused as invalid input naming the file and the key it concerns.
func TestAMalformedObjectivesFileIsRefusedNamingTheKey(t *testing.T) {
	two := "[[objective]]\nname = \"a\"\ndirection = \"max\"\n[[objective]]\nname = \"b\"\ndirection = \"min\"\n"
	cases := map[string]struct{ text, key string }{
		"no name":          {"[[objective]]\ndirection = \"max\"\n", "objective[0].name"},
		"an empty name":    {"[[objective]]\nname = \"\"\ndirection = \"max\"\n", "objective[0].name"},
		"no direction":     {"[[objective]]\nname = \"a\"\n", "objective[0].direction"},
		"a bad direction":  {"[[objective]]\nname = \"a\"\ndirection = \"up\"\n", "objective[0].direction"},
		"a numeric name":   {"[[objective]]\nname = 3\ndirection = \"max\"\n", "objective[0].name"},
		"objective a key":  {"objective = 3\n", "objective"},
		"pareto a string":  {"pareto = \"x\"\n", "pareto"},
		"a duplicate":      {two + "[[objective]]\nname = \"a\"\ndirection = \"min\"\n", `"a"`},
		"noise 1":          {two + "[pareto]\nrelative_noise = 1\n", "pareto.relative_noise"},
		"noise negative":   {two + "[pareto]\nrelative_noise = -0.5\n", "pareto.relative_noise"},
		"noise nan":        {two + "[pareto]\nrelative_noise = nan\n", "pareto.relative_noise"},
		"noise inf":        {two + "[pareto]\nrelative_noise = inf\n", "pareto.relative_noise"},
		"noise a string":   {two + "[pareto]\nrelative_noise = \"0.1\"\n", "pareto.relative_noise"},
		"own noise 1":      {strings.Replace(two, "name = \"b\"", "name = \"b\"\nrelative_noise = 1.0", 1) + "[pareto]\n", `"b"`},
		"own noise nan":    {strings.Replace(two, "name = \"b\"", "name = \"b\"\nrelative_noise = nan", 1) + "[pareto]\n", `"b"`},
		"own noise alone":  {strings.Replace(two, "name = \"b\"", "name = \"b\"\nrelative_noise = 0.1", 1), `"b"`},
		"own noise, one":   {"[[objective]]\nname = \"a\"\ndirection = \"max\"\nrelative_noise = 0.1\n[pareto]\n", `"a"`},
		"not toml":         {"this is not toml", ""},
		"a repeated key":   {"[pareto]\nrelative_noise = 0.1\nrelative_noise = 0.2\n", "relative_noise"},
		"a misspelt noise": {"[[objective]]\nname = \"a\"\ndirection = \"max\"\nrelative_noize = 0.1\n", "relative_noize"},
	}
	for name, c := range cases {
		_, err := task.ParseObjectives([]byte(c.text))
		if err == nil {
			t.Errorf("%s: accepted", name)
			continue
		}
		f := failureOf(t, err)
		if f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, task.ObjectiveName) || !strings.Contains(f.Message, c.key) {
			t.Errorf("%s: %q (%s), want invalid input naming %s and %q", name, f.Message, f.Kind, task.ObjectiveName, c.key)
		}
	}
}

// A noise floor outside [0, 1) is refused wherever it is, and one inside it is read back as written.
func TestANoiseFloorIsReadBackOrRefusedNamingTheObjective(t *testing.T) {
	r := rand.New(rand.NewPCG(11, 12))
	for range 500 {
		v := math.Float64frombits(r.Uint64())
		if r.IntN(2) == 0 {
			v = r.Float64()*4 - 1
		}
		if math.IsNaN(v) || math.IsInf(v, 0) {
			continue
		}
		text := "[[objective]]\nname = \"a\"\ndirection = \"max\"\n[[objective]]\nname = \"b\"\ndirection = \"min\"\nrelative_noise = " +
			strconv.FormatFloat(v, 'g', -1, 64) + "\n[pareto]\n"
		got, err := task.ParseObjectives([]byte(text))
		if v >= 0 && v < 1 {
			if err != nil || got.GetObjective()[1].GetRelativeNoise() != v {
				t.Fatalf("%v: %v, %v", v, got, err)
			}
			continue
		}
		if err == nil || !strings.Contains(err.Error(), `"b"`) || !strings.Contains(err.Error(), "relative_noise") {
			t.Fatalf("%v: %v, want a refusal naming b and relative_noise", v, err)
		}
	}
}

func FuzzParseObjectives(f *testing.F) {
	f.Add([]byte(objectives))
	f.Add([]byte(objectives2))
	f.Add([]byte(fmt.Sprintf(noiseObjectives, "0.1")))
	f.Add([]byte("[pareto]\nrelative_noise = nan\n"))
	f.Add([]byte(""))
	f.Fuzz(func(t *testing.T, data []byte) {
		got, err := task.ParseObjectives(data)
		if err != nil {
			if fail := failureOf(t, err); fail.Message == "" || !strings.Contains(fail.Message, task.ObjectiveName) {
				t.Fatalf("error %q does not name the file", fail.Message)
			}
			return
		}
		// What is accepted is a message that is written as the file and read back as itself.
		again, err := task.ParseObjectives([]byte(render(got)))
		if err != nil || !proto.Equal(got, again) {
			t.Fatalf("%v written as\n%s\nreads as %v, %v", got, render(got), again, err)
		}
	})
}
