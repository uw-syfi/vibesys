// Package tomlproto decodes a TOML file into a protobuf message by the message's field names: a
// key the message has no field for, and a value of the wrong type, are refused here, naming the
// key, and not by protojson, which names neither. An enum value is written as its name without the
// enum's prefix, in lower case. The operator config and the task manifest are read with it, so
// each has one schema, its .proto, and no hand-kept decoder.
//
// A message that is a task manifest also says, field by field, which minor version of its schema
// introduced the field (task_schema_since_minor in task.proto). Given the version a file declares,
// Decode refuses a key newer than it, naming the key and the version it needs.
package tomlproto

import (
	"encoding/json"
	"fmt"
	"maps"
	"slices"
	"strings"

	"github.com/BurntSushi/toml"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Version is a schema version, MAJOR.MINOR.
type Version struct{ Major, Minor uint32 }

// String implements fmt.Stringer.
func (v Version) String() string { return fmt.Sprintf("%d.%d", v.Major, v.Minor) }

// Options says how Decode reads a file.
type Options struct {
	// Declared, when set, is the schema version the file declares: a key whose field a later
	// minor version introduced is refused. Nil checks no field's version.
	Declared *Version
}

// Decode decodes TOML text into m. It fails with a *core.Failure of kind FailureInvalidInput that
// names the offending key.
func Decode(data []byte, m proto.Message, o Options) error {
	var raw map[string]any
	if _, err := toml.Decode(string(data), &raw); err != nil {
		return invalid("%v", err)
	}
	return DecodeTable(raw, m, o)
}

// DecodeTable decodes an already parsed TOML table into m, as Decode does.
func DecodeTable(table map[string]any, m proto.Message, o Options) error {
	d := decoder{o: o}
	j, err := d.message(table, m.ProtoReflect().Descriptor(), "")
	if err != nil {
		return err
	}
	text, err := json.Marshal(j)
	if err != nil {
		return invalid("%v", err)
	}
	if err := protojson.Unmarshal(text, m); err != nil {
		return invalid("%v", err)
	}
	return nil
}

// Since is the minor version that introduced the field (task_schema_since_minor), and false for
// a field that names none.
func Since(fd protoreflect.FieldDescriptor) (uint32, bool) {
	opts := fd.Options()
	if opts == nil || !proto.HasExtension(opts, cliv1.E_TaskSchemaSinceMinor) {
		return 0, false
	}
	return proto.GetExtension(opts, cliv1.E_TaskSchemaSinceMinor).(uint32), true
}

// Schema is the schema version a manifest message is (task_schema), and false for a message that
// names none.
func Schema(md protoreflect.MessageDescriptor) (Version, bool) {
	opts := md.Options()
	if opts == nil || !proto.HasExtension(opts, cliv1.E_TaskSchema) {
		return Version{}, false
	}
	s := proto.GetExtension(opts, cliv1.E_TaskSchema).(*cliv1.TaskSchema)
	return Version{Major: s.GetMajor(), Minor: s.GetMinor()}, true
}

func invalid(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(format, args...)}
}

type decoder struct{ o Options }

// message turns a decoded TOML table into the JSON protojson reads for the message md describes,
// field by field.
func (d decoder) message(table map[string]any, md protoreflect.MessageDescriptor, at string) (map[string]any, error) {
	out := make(map[string]any, len(table))
	for _, key := range slices.Sorted(maps.Keys(table)) {
		here := join(at, key)
		fd := md.Fields().ByName(protoreflect.Name(key))
		if fd == nil || string(fd.Name()) != key {
			return nil, invalid("unknown key %q", here)
		}
		if v := d.o.Declared; v != nil {
			if since, ok := Since(fd); ok && since > v.Minor {
				need := Version{Major: v.Major, Minor: since}
				return nil, invalid("the key %q needs task schema %s, and the file declares %s; declare version = %q", here, need, *v, need.String())
			}
		}
		v, err := d.field(table[key], fd, here)
		if err != nil {
			return nil, err
		}
		out[key] = v
	}
	return out, nil
}

func (d decoder) field(v any, fd protoreflect.FieldDescriptor, at string) (any, error) {
	switch {
	case fd.IsMap():
		table, ok := v.(map[string]any)
		if !ok {
			return nil, invalid("%s: want a table, got %v", at, v)
		}
		out := make(map[string]any, len(table))
		for _, k := range slices.Sorted(maps.Keys(table)) {
			x, err := d.single(table[k], fd.MapValue(), join(at, k))
			if err != nil {
				return nil, err
			}
			out[k] = x
		}
		return out, nil
	case fd.IsList():
		var items []any
		switch l := v.(type) {
		case []any:
			items = l
		case []map[string]any:
			for _, t := range l {
				items = append(items, t)
			}
		default:
			return nil, invalid("%s: want a list, got %v", at, v)
		}
		out := make([]any, len(items))
		for i, x := range items {
			y, err := d.single(x, fd, fmt.Sprintf("%s[%d]", at, i))
			if err != nil {
				return nil, err
			}
			out[i] = y
		}
		return out, nil
	}
	return d.single(v, fd, at)
}

// single converts one value of a field's kind.
func (d decoder) single(v any, fd protoreflect.FieldDescriptor, at string) (any, error) {
	wrong := func(want string) error { return invalid("%s: want %s, got %v", at, want, v) }
	switch fd.Kind() {
	case protoreflect.MessageKind:
		table, ok := v.(map[string]any)
		if !ok {
			return nil, wrong("a table")
		}
		if fd.Message().FullName() == structName {
			// A section held as data: any keys, read by someone else.
			return opaque(table), nil
		}
		return d.message(table, fd.Message(), at)
	case protoreflect.EnumKind:
		name, ok := v.(string)
		if !ok {
			return nil, wrong("a name")
		}
		values := fd.Enum().Values()
		prefix := strings.TrimSuffix(string(values.ByNumber(0).Name()), "UNSPECIFIED")
		var names []string
		for i := 1; i < values.Len(); i++ {
			names = append(names, strings.ToLower(strings.TrimPrefix(string(values.Get(i).Name()), prefix)))
		}
		if !slices.Contains(names, name) {
			return nil, invalid("%s: %q is none of %s", at, name, strings.Join(names, ", "))
		}
		return prefix + strings.ToUpper(name), nil
	case protoreflect.StringKind:
		if _, ok := v.(string); !ok {
			return nil, wrong("a string")
		}
	case protoreflect.BoolKind:
		if _, ok := v.(bool); !ok {
			return nil, wrong("true or false")
		}
	case protoreflect.Int32Kind, protoreflect.Int64Kind, protoreflect.Uint32Kind, protoreflect.Uint64Kind:
		n, ok := v.(int64)
		if !ok {
			return nil, wrong("an integer")
		}
		if n < 0 && (fd.Kind() == protoreflect.Uint32Kind || fd.Kind() == protoreflect.Uint64Kind) {
			return nil, wrong("a number that is not negative")
		}
	case protoreflect.DoubleKind, protoreflect.FloatKind:
		switch n := v.(type) {
		case float64:
		case int64:
			return float64(n), nil
		default:
			return nil, wrong("a number")
		}
	default:
		return nil, invalid("%s: a field of kind %s cannot be written in the file", at, fd.Kind())
	}
	return v, nil
}

// structName is the message a section held as data is: its keys are not checked.
const structName protoreflect.FullName = "google.protobuf.Struct"

// opaque is a TOML value as JSON: a date or a time becomes its TOML text.
func opaque(v any) any {
	switch x := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, y := range x {
			out[k] = opaque(y)
		}
		return out
	case []map[string]any:
		out := make([]any, len(x))
		for i, y := range x {
			out[i] = opaque(y)
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, y := range x {
			out[i] = opaque(y)
		}
		return out
	case string, bool, int64, float64:
		return x
	case fmt.Stringer:
		return x.String()
	}
	return fmt.Sprint(v)
}

func join(at, key string) string {
	if at == "" {
		return key
	}
	return at + "." + key
}
