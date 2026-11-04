package operator

import (
	"encoding/json"
	"fmt"
	"maps"
	"slices"
	"strings"

	"google.golang.org/protobuf/reflect/protoreflect"
)

// message turns a decoded TOML table into the JSON protojson reads for the message md describes,
// field by field, so that a key with no field and a value of the wrong type are refused here,
// naming the key, and not by protojson, which names neither.
func message(table map[string]any, md protoreflect.MessageDescriptor, at string) (map[string]any, error) {
	out := make(map[string]any, len(table))
	for _, key := range slices.Sorted(maps.Keys(table)) {
		here := join(at, key)
		fd := md.Fields().ByName(protoreflect.Name(key))
		if fd == nil || string(fd.Name()) != key {
			return nil, invalid("unknown key %q", here)
		}
		v, err := field(table[key], fd, here)
		if err != nil {
			return nil, err
		}
		out[key] = v
	}
	return out, nil
}

func field(v any, fd protoreflect.FieldDescriptor, at string) (any, error) {
	switch {
	case fd.IsMap():
		table, ok := v.(map[string]any)
		if !ok {
			return nil, invalid("%s: want a table, got %v", at, v)
		}
		out := make(map[string]any, len(table))
		for _, k := range slices.Sorted(maps.Keys(table)) {
			x, err := single(table[k], fd.MapValue(), join(at, k))
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
			y, err := single(x, fd, fmt.Sprintf("%s[%d]", at, i))
			if err != nil {
				return nil, err
			}
			out[i] = y
		}
		return out, nil
	}
	return single(v, fd, at)
}

// single converts one value of a field's kind.
func single(v any, fd protoreflect.FieldDescriptor, at string) (any, error) {
	wrong := func(want string) error { return invalid("%s: want %s, got %v", at, want, v) }
	switch fd.Kind() {
	case protoreflect.MessageKind:
		table, ok := v.(map[string]any)
		if !ok {
			return nil, wrong("a table")
		}
		return message(table, fd.Message(), at)
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

func join(at, key string) string {
	if at == "" {
		return key
	}
	return at + "." + key
}

func jsonOf(v map[string]any) ([]byte, error) { return json.Marshal(v) }
