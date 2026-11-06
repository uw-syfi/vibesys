package task

import (
	"errors"
	"slices"
	"strings"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/tomlproto"
)

// ParseObjectives reads objectives.toml as TaskObjectives (task.proto) says it is: every key is a
// field of the message, a key it has no field for is refused, and the rules stated on the message
// hold. A nil file is a task with no objectives. A refusal is an invalid-input failure that names
// the file and the key.
func ParseObjectives(data []byte) (*cliv1.TaskObjectives, error) {
	o := &cliv1.TaskObjectives{}
	if data == nil {
		return o, nil
	}
	if err := tomlproto.Decode(data, o, tomlproto.Options{}); err != nil {
		return nil, invalid("%s: %s", ObjectiveName, core.AsFailure(err).Message)
	}
	if err := validate(ObjectiveName, o); err != nil {
		return nil, err
	}
	return o, nil
}

// validate holds a decoded file to the protovalidate rules of its message, naming the file and
// the path of each key that breaks one.
func validate(file string, m proto.Message) error {
	err := protovalidate.Validate(m)
	if err == nil {
		return nil
	}
	var broken *protovalidate.ValidationError
	if !errors.As(err, &broken) {
		return &core.Failure{Kind: core.FailureInternal, Message: file + ": checking it against its rules: " + err.Error()}
	}
	found := make([]string, 0, len(broken.Violations))
	for _, v := range broken.Violations {
		msg := v.Proto.GetMessage()
		if path := protovalidate.FieldPathString(v.Proto.GetField()); path != "" {
			msg = path + ": " + msg
		}
		found = append(found, msg)
	}
	slices.Sort(found)
	return invalid("%s: %s", file, strings.Join(found, "; "))
}

// axis is an objective as the reader of a result uses it.
type axis struct {
	name      string
	direction core.Direction
	noise     *float64 // the objective's own relative_noise, nil when it declares none
}

// axesOf is the objectives of a valid file, in the order it lists them.
func axesOf(o *cliv1.TaskObjectives) []axis {
	axes := make([]axis, 0, len(o.GetObjective()))
	for _, obj := range o.GetObjective() {
		d := core.Maximize
		if obj.GetDirection() == cliv1.TaskObjectiveDirection_TASK_OBJECTIVE_DIRECTION_MIN {
			d = core.Minimize
		}
		axes = append(axes, axis{name: obj.GetName(), direction: d, noise: obj.RelativeNoise})
	}
	return axes
}
