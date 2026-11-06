// Package jobenv reads what a job's process is told about the job it runs in: the fields of
// JobEnvironment and KubernetesEnvironment (proto/vibesys/cli/v1/env.proto), which vibesys-cli writes
// as environment variables. The variable a field travels in is its env_name option, so no code
// here names a variable; the rules the messages state are the ones checked, and a bad value is
// refused with an error that names its variable and never repeats the value.
//
// A string field is its value, an unsigned integer is its decimal digits and a repeated string is
// its elements separated by commas. A variable that is unset or empty is the field's zero value;
// the rules say whether that is allowed. This is the reading half of vibesys-cli's cli/envcodec, and
// a corpus both are held to (proto/vibesys/cli/v1/conformance/environment.json) keeps them one
// format.
//
// The package reads no process state: its caller hands it a Lookup.
package jobenv

import (
	"errors"
	"fmt"
	"slices"
	"strconv"
	"strings"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	cliv1 "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/cli/v1"
)

// Lookup answers the value of a variable, empty when it is not set.
type Lookup func(name string) string

// Error is a value that cannot be what its variable is for.
type Error struct {
	Variable string
	Reason   string
}

func (e *Error) Error() string { return e.Variable + ": " + e.Reason }

// Job reads the job environment.
func Job(lookup Lookup) (*cliv1.JobEnvironment, error) {
	m := &cliv1.JobEnvironment{}
	return m, Decode(lookup, m)
}

// Kubernetes reads the cluster a job that needs one is given.
func Kubernetes(lookup Lookup) (*cliv1.KubernetesEnvironment, error) {
	m := &cliv1.KubernetesEnvironment{}
	return m, Decode(lookup, m)
}

// NameOf is the variable a field of m is carried in, for a message that tells a person which one
// to set. It is empty when m has no such field.
func NameOf(m proto.Message, field protoreflect.Name) string {
	f := m.ProtoReflect().Descriptor().Fields().ByName(field)
	if f == nil {
		return ""
	}
	name, _ := proto.GetExtension(f.Options(), cliv1.E_EnvName).(string)
	return name
}

// Decode reads m's fields from lookup, then holds m to its rules. It returns every error it
// finds, each an *Error naming its variable.
func Decode(lookup Lookup, m proto.Message) error {
	r := m.ProtoReflect()
	fields := r.Descriptor().Fields()
	names := map[string]string{}
	var problems []error
	for i := range fields.Len() {
		f := fields.Get(i)
		name := NameOf(m, f.Name())
		if name == "" {
			return fmt.Errorf("jobenv: field %s has no env_name", f.FullName())
		}
		names[string(f.Name())] = name
		text := lookup(name)
		if text == "" {
			continue
		}
		value, err := parse(r, f, text)
		if err != nil {
			problems = append(problems, &Error{Variable: name, Reason: err.Error()})
			continue
		}
		r.Set(f, value)
	}
	if len(problems) > 0 {
		return errors.Join(problems...)
	}
	return holdToRules(m, names)
}

func parse(m protoreflect.Message, f protoreflect.FieldDescriptor, text string) (protoreflect.Value, error) {
	switch {
	case f.IsList() && f.Kind() == protoreflect.StringKind:
		list := m.NewField(f)
		for _, element := range strings.Split(text, ",") {
			list.List().Append(protoreflect.ValueOfString(element))
		}
		return list, nil
	case f.Kind() == protoreflect.StringKind:
		return protoreflect.ValueOfString(text), nil
	case f.Kind() == protoreflect.Uint32Kind:
		n, err := strconv.ParseUint(text, 10, 32)
		if err != nil {
			return protoreflect.Value{}, errors.New("not an unsigned 32-bit number")
		}
		return protoreflect.ValueOfUint32(uint32(n)), nil
	}
	return protoreflect.Value{}, fmt.Errorf("a variable holds a string, a list of strings or a 32-bit unsigned integer, not a %s", f.Kind())
}

func holdToRules(m proto.Message, names map[string]string) error {
	err := protovalidate.Validate(m)
	if err == nil {
		return nil
	}
	var broken *protovalidate.ValidationError
	if !errors.As(err, &broken) {
		return fmt.Errorf("jobenv: checking %s against its rules: %w", m.ProtoReflect().Descriptor().FullName(), err)
	}
	var problems []error
	for _, v := range broken.Violations {
		variable := string(m.ProtoReflect().Descriptor().Name())
		if path := v.Proto.GetField().GetElements(); len(path) > 0 {
			if name, ok := names[path[0].GetFieldName()]; ok {
				variable = name
			}
		}
		problems = append(problems, &Error{Variable: variable, Reason: v.Proto.GetMessage()})
	}
	slices.SortFunc(problems, func(a, b error) int { return strings.Compare(a.Error(), b.Error()) })
	return errors.Join(problems...)
}
