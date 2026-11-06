// Package envcodec writes a message as environment variables and reads one back. The variable a
// field travels in is its env_name option (proto/vibesys/cli/v1/env.proto), so the definition of a
// variable is one field of one message, and a reader holds a message and not a name.
//
// Encode is total over the messages this package supports: a field is a string, a list of strings
// (written as its elements separated by commas), an unsigned integer or an enum, and has an
// env_name. A field at its zero value is not set, so Encode says
// which names to unset as well. Decode reads the names a message declares, holds the result to the
// message's protovalidate rules, and refuses what breaks them with an error that names the
// variable. A value is never part of an error, because some variables hold secrets.
//
// The package reads no process state: its caller hands it the environment, a Lookup, so a test
// supplies a Fake and the callers that may read the real one stay few and listed.
package envcodec

import (
	"errors"
	"fmt"
	"slices"
	"sort"
	"strconv"
	"strings"
	"unicode"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// Lookup answers the value of a variable, empty when it is not set.
type Lookup func(name string) string

// FromEnviron is the Lookup of an environment in the form os.Environ returns it. The last of a
// repeated name wins, as it does for a process that starts with that environment.
func FromEnviron(environ []string) Lookup {
	values := make(map[string]string, len(environ))
	for _, entry := range environ {
		if name, value, ok := strings.Cut(entry, "="); ok {
			values[name] = value
		}
	}
	return func(name string) string { return values[name] }
}

// Environment is what a message becomes: the names to set with their values, and the names whose
// field is empty, which the receiver's environment must not hold so that its program resolves them
// as it would without the sender.
type Environment struct {
	Set   map[string]string
	Unset []string
}

// Error is a value that cannot be what its variable is for. It names the variable and says why, and
// never repeats the value.
type Error struct {
	Variable string
	Reason   string
}

func (e *Error) Error() string { return e.Variable + ": " + e.Reason }

// Encode writes m as an Environment, after holding m to its rules, so a sender cannot write what a
// reader would refuse. A field that is not a string, an unsigned integer or an enum, or that has no
// env_name, is an error, because the message is not one this codec carries.
func Encode(m proto.Message) (Environment, error) {
	out := Environment{Set: map[string]string{}}
	fields, err := declared(m.ProtoReflect().Descriptor())
	if err != nil {
		return Environment{}, err
	}
	r := m.ProtoReflect()
	for _, f := range fields {
		if !r.Has(f.field) {
			out.Unset = append(out.Unset, f.name)
			continue
		}
		text, err := format(f.field, r.Get(f.field))
		if err != nil {
			return Environment{}, &Error{Variable: f.name, Reason: err.Error()}
		}
		out.Set[f.name] = text
	}
	if err := holdToRules(m, fields); err != nil {
		return Environment{}, err
	}
	return out, nil
}

// Decode reads m's fields from lookup, then holds m to its rules. A variable that is unset or empty
// is the field's zero value; whether that is allowed is the rules' to say. It returns every error
// it finds, each naming its variable.
func Decode(lookup Lookup, m proto.Message) error {
	fields, err := declared(m.ProtoReflect().Descriptor())
	if err != nil {
		return err
	}
	r := m.ProtoReflect()
	var problems []error
	for _, f := range fields {
		text := lookup(f.name)
		if text == "" {
			r.Clear(f.field)
			continue
		}
		value, err := parse(r, f.field, text)
		if err != nil {
			problems = append(problems, &Error{Variable: f.name, Reason: err.Error()})
			continue
		}
		r.Set(f.field, value)
	}
	if len(problems) > 0 {
		return errors.Join(problems...)
	}
	return holdToRules(m, fields)
}

// Unknown lists the names in environ that start with m's env_namespace and are not a field of m,
// in order. It is empty when m declares no namespace. The decoder does not refuse them: an
// inherited environment can hold a name in the prefix that is another program's setting. A caller
// that wants a misspelled name noticed reports these.
func Unknown(environ []string, m proto.Message) []string {
	prefix, _ := proto.GetExtension(m.ProtoReflect().Descriptor().Options(), cliv1.E_EnvNamespace).(string)
	if prefix == "" {
		return nil
	}
	fields, err := declared(m.ProtoReflect().Descriptor())
	if err != nil {
		return nil
	}
	known := map[string]bool{}
	for _, f := range fields {
		known[f.name] = true
	}
	found := map[string]bool{}
	for _, entry := range environ {
		if name, _, ok := strings.Cut(entry, "="); ok && strings.HasPrefix(name, prefix) && !known[name] {
			found[name] = true
		}
	}
	out := make([]string, 0, len(found))
	for name := range found {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// Names lists the variables m's definition declares, in field order.
func Names(m proto.Message) ([]string, error) {
	fields, err := declared(m.ProtoReflect().Descriptor())
	if err != nil {
		return nil, err
	}
	names := make([]string, len(fields))
	for i, f := range fields {
		names[i] = f.name
	}
	return names, nil
}

// NameOf is the variable a field of m is carried in, for a message that tells a person which one to
// set. It is empty when m has no such field, which a test of the caller catches.
func NameOf(m proto.Message, field protoreflect.Name) string {
	f := m.ProtoReflect().Descriptor().Fields().ByName(field)
	if f == nil {
		return ""
	}
	name, _ := proto.GetExtension(f.Options(), cliv1.E_EnvName).(string)
	return name
}

type variable struct {
	name  string
	field protoreflect.FieldDescriptor
}

func declared(d protoreflect.MessageDescriptor) ([]variable, error) {
	var out []variable
	for i := 0; i < d.Fields().Len(); i++ {
		f := d.Fields().Get(i)
		name, _ := proto.GetExtension(f.Options(), cliv1.E_EnvName).(string)
		if name == "" {
			return nil, fmt.Errorf("envcodec: field %s has no env_name; every field of an environment message carries one", f.FullName())
		}
		switch {
		case f.IsMap() || (f.IsList() && f.Kind() != protoreflect.StringKind):
			return nil, fmt.Errorf("envcodec: field %s is a %s; a variable holds one value or a list of strings", f.FullName(), "map or repeated "+f.Kind().String())
		case f.Kind() == protoreflect.StringKind, f.Kind() == protoreflect.Uint32Kind, f.Kind() == protoreflect.Uint64Kind, f.Kind() == protoreflect.EnumKind:
		default:
			return nil, fmt.Errorf("envcodec: field %s is a %s; a variable holds a string, an unsigned integer or an enum", f.FullName(), f.Kind())
		}
		out = append(out, variable{name: name, field: f})
	}
	return out, nil
}

func format(f protoreflect.FieldDescriptor, v protoreflect.Value) (string, error) {
	if f.IsList() {
		list := v.List()
		elements := make([]string, list.Len())
		for i := range elements {
			elements[i] = list.Get(i).String()
			if strings.Contains(elements[i], ",") {
				return "", errors.New("an element holds a comma")
			}
		}
		return strings.Join(elements, ","), nil
	}
	switch f.Kind() {
	case protoreflect.StringKind:
		return v.String(), nil
	case protoreflect.Uint32Kind, protoreflect.Uint64Kind:
		return strconv.FormatUint(v.Uint(), 10), nil
	default:
		value := f.Enum().Values().ByNumber(v.Enum())
		if value == nil {
			return "", fmt.Errorf("%d is not a value of %s", v.Enum(), f.Enum().Name())
		}
		return spelling(f.Enum(), value), nil
	}
}

func parse(m protoreflect.Message, f protoreflect.FieldDescriptor, text string) (protoreflect.Value, error) {
	if f.IsList() {
		list := m.NewField(f)
		for _, element := range strings.Split(text, ",") {
			list.List().Append(protoreflect.ValueOfString(element))
		}
		return list, nil
	}
	switch f.Kind() {
	case protoreflect.StringKind:
		return protoreflect.ValueOfString(text), nil
	case protoreflect.Uint32Kind:
		n, err := strconv.ParseUint(text, 10, 32)
		if err != nil {
			return protoreflect.Value{}, errors.New("not an unsigned 32-bit number")
		}
		return protoreflect.ValueOfUint32(uint32(n)), nil
	case protoreflect.Uint64Kind:
		n, err := strconv.ParseUint(text, 10, 64)
		if err != nil {
			return protoreflect.Value{}, errors.New("not an unsigned 64-bit number")
		}
		return protoreflect.ValueOfUint64(n), nil
	default:
		values := f.Enum().Values()
		for i := 0; i < values.Len(); i++ {
			if spelling(f.Enum(), values.Get(i)) == text {
				return protoreflect.ValueOfEnum(values.Get(i).Number()), nil
			}
		}
		return protoreflect.Value{}, fmt.Errorf("not one of %s", strings.Join(spellings(f.Enum()), ", "))
	}
}

// spelling is an enum value as a variable holds it: its name without the enum's prefix, in lower case.
func spelling(e protoreflect.EnumDescriptor, v protoreflect.EnumValueDescriptor) string {
	prefix := snake(string(e.Name())) + "_"
	return strings.ToLower(strings.TrimPrefix(strings.ToUpper(string(v.Name())), prefix))
}

// spellings lists the spellings an enum can be read from, without the unspecified zero.
func spellings(e protoreflect.EnumDescriptor) []string {
	var out []string
	for i := 0; i < e.Values().Len(); i++ {
		if v := e.Values().Get(i); v.Number() != 0 {
			out = append(out, spelling(e, v))
		}
	}
	return out
}

// snake spells a CamelCase name in upper snake case: AgentRole is AGENT_ROLE.
func snake(name string) string {
	var b strings.Builder
	for i, r := range name {
		if i > 0 && unicode.IsUpper(r) {
			b.WriteByte('_')
		}
		b.WriteRune(unicode.ToUpper(r))
	}
	return b.String()
}

// holdToRules validates m and names the variable of each field that breaks a rule. A rule on the
// message as a whole has no variable, so it is reported under the message's name.
func holdToRules(m proto.Message, fields []variable) error {
	err := protovalidate.Validate(m)
	if err == nil {
		return nil
	}
	var broken *protovalidate.ValidationError
	if !errors.As(err, &broken) {
		return fmt.Errorf("envcodec: checking %s against its rules: %w", m.ProtoReflect().Descriptor().FullName(), err)
	}
	var problems []error
	for _, v := range broken.Violations {
		name := string(m.ProtoReflect().Descriptor().Name())
		if path := v.Proto.GetField().GetElements(); len(path) > 0 {
			if i := slices.IndexFunc(fields, func(f variable) bool { return string(f.field.Name()) == path[0].GetFieldName() }); i >= 0 {
				name = fields[i].name
			}
		}
		problems = append(problems, &Error{Variable: name, Reason: v.Proto.GetMessage()})
	}
	slices.SortFunc(problems, func(a, b error) int { return strings.Compare(a.Error(), b.Error()) })
	return errors.Join(problems...)
}
