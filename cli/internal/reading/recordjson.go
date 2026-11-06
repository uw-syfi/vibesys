package reading

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"math"
	"strconv"

	"google.golang.org/protobuf/reflect/protoreflect"
)

// This file reads one JSON object into a generated message, strictly. It exists because proto3
// JSON is lenient where the protocol is not: it accepts a number written as a string ("1.5"),
// integers written with a fraction, lowerCamelCase aliases, and unknown keys when asked to
// ignore them. A benchmark's output is untrusted, and the number it reports decides a promotion,
// so a key or a value the definition does not allow is a problem with a name, never a guess.

// problemKind says how a key failed to fit the message.
type problemKind int

const (
	// unknownKey is a key the message does not define.
	unknownKey problemKind = iota
	// wrongType is a value of another JSON type than the field's, or an integer out of range.
	wrongType
	// notFinite is a number that overflows a float64.
	notFinite
)

// problem is one key that did not fit, named by its path (for example "metrics.m.color").
type problem struct {
	path string
	kind problemKind
	// want says what the key should have been, as "a string" or "a finite number".
	want string
}

// notObject says the text was JSON that is not an object; it is a syntax error otherwise.
var errNotObject = errors.New("not a JSON object")

// decodeObject parses line as one JSON object and fills msg from it, returning every key that did
// not fit. A JSON null is an absent key, as in proto3 JSON, except as a map entry's value, where
// it is a value of the wrong type. A key that fits is kept even when others do not, so a caller
// can still read the record's kind to decide which problem to report first.
func decodeObject(line []byte, msg protoreflect.Message) ([]problem, error) {
	dec := json.NewDecoder(bytes.NewReader(line))
	dec.UseNumber()
	var top any
	if err := dec.Decode(&top); err != nil {
		return nil, err
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return nil, errors.New("trailing data")
	}
	obj, ok := top.(map[string]any)
	if !ok {
		return nil, errNotObject
	}
	var out []problem
	fillMessage(msg, obj, "", &out)
	return out, nil
}

func join(path, key string) string {
	if path == "" {
		return key
	}
	return path + "." + key
}

func fillMessage(msg protoreflect.Message, obj map[string]any, path string, out *[]problem) {
	fields := msg.Descriptor().Fields()
	for _, key := range keys(obj) {
		here := join(path, key)
		fd := fields.ByName(protoreflect.Name(key))
		if fd == nil {
			*out = append(*out, problem{here, unknownKey, ""})
			continue
		}
		v := obj[key]
		if v == nil {
			continue
		}
		switch {
		case fd.IsMap():
			fillMap(msg.Mutable(fd).Map(), fd, v, here, out)
		case fd.Kind() == protoreflect.MessageKind:
			sub, ok := v.(map[string]any)
			if !ok {
				*out = append(*out, problem{here, wrongType, "an object"})
				continue
			}
			fillMessage(msg.Mutable(fd).Message(), sub, here, out)
		default:
			val, bad, ok := scalar(fd.Kind(), v)
			if !ok {
				bad.path = here
				*out = append(*out, bad)
				continue
			}
			msg.Set(fd, val)
		}
	}
}

func fillMap(m protoreflect.Map, fd protoreflect.FieldDescriptor, v any, path string, out *[]problem) {
	obj, ok := v.(map[string]any)
	if !ok {
		*out = append(*out, problem{path, wrongType, "an object"})
		return
	}
	valueField := fd.MapValue()
	for _, key := range keys(obj) {
		here := join(path, key)
		entry := obj[key]
		if valueField.Kind() == protoreflect.MessageKind {
			sub, ok := entry.(map[string]any)
			if !ok {
				*out = append(*out, problem{here, wrongType, "an object"})
				continue
			}
			value := m.NewValue()
			fillMessage(value.Message(), sub, here, out)
			m.Set(protoreflect.ValueOfString(key).MapKey(), value)
			continue
		}
		val, bad, ok := scalar(valueField.Kind(), entry)
		if !ok {
			bad.path = here
			*out = append(*out, bad)
			continue
		}
		m.Set(protoreflect.ValueOfString(key).MapKey(), val)
	}
}

// scalar converts a decoded JSON value to the scalar kind, or returns the problem (without its
// path) that says how it does not fit. The protocol uses only the kinds below; any other is a
// definition this reader was not written for.
func scalar(kind protoreflect.Kind, v any) (protoreflect.Value, problem, bool) {
	var want string
	switch kind {
	case protoreflect.StringKind:
		want = "a string"
		if s, ok := v.(string); ok {
			return protoreflect.ValueOfString(s), problem{}, true
		}
	case protoreflect.BoolKind:
		want = "a boolean"
		if b, ok := v.(bool); ok {
			return protoreflect.ValueOfBool(b), problem{}, true
		}
	case protoreflect.Uint32Kind:
		// An integer token: a fraction or an exponent makes it another number.
		want = "an integer"
		if n, ok := v.(json.Number); ok {
			if i, err := strconv.ParseUint(n.String(), 10, 32); err == nil {
				return protoreflect.ValueOfUint32(uint32(i)), problem{}, true
			}
		}
	case protoreflect.DoubleKind:
		want = "a finite number"
		if n, ok := v.(json.Number); ok {
			f, err := strconv.ParseFloat(n.String(), 64)
			if err != nil || math.IsInf(f, 0) || math.IsNaN(f) {
				return protoreflect.Value{}, problem{kind: notFinite, want: want}, false
			}
			return protoreflect.ValueOfFloat64(f), problem{}, true
		}
	default:
		panic("reading: the result record defines a field kind the reader does not handle: " + kind.String())
	}
	return protoreflect.Value{}, problem{kind: wrongType, want: want}, false
}
