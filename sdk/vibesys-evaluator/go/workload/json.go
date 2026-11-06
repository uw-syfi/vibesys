package workload

import (
	"fmt"
	"math"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"
)

// Object is a JSON object whose keys keep their order, for a JSON body sent with the same bytes
// as the Python SDK sends a dict ({"user": u, "password": p} is Object{{"user", u}, {"password",
// p}}). A Go map is sent with its keys sorted; a struct with its fields in order, named by their
// `json` tags.
type Object []Field

// Field is one key and value of an Object.
type Field struct {
	Key   string
	Value any
}

// jsonParts appends the JSON text of value as Python's json.dumps writes it compactly
// (separators "," and ":", non-ASCII escaped): literal text and the refs of Templates, which
// become JSON strings with the refs inside.
func jsonParts(value any, out *[]part, where string) {
	switch v := value.(type) {
	case nil:
		appendLiteral(out, "null")
	case Template:
		appendLiteral(out, `"`)
		for _, p := range v.parts {
			if p.ref != "" {
				*out = append(*out, p)
			} else {
				quoted := quotePython(p.literal)
				appendLiteral(out, quoted[1:len(quoted)-1])
			}
		}
		appendLiteral(out, `"`)
	case Object:
		appendLiteral(out, "{")
		for i, field := range v {
			if i > 0 {
				appendLiteral(out, ",")
			}
			appendLiteral(out, quotePython(field.Key)+":")
			jsonParts(field.Value, out, where+"."+field.Key)
		}
		appendLiteral(out, "}")
	case string:
		appendLiteral(out, quotePython(v))
	case bool:
		appendLiteral(out, strconv.FormatBool(v))
	case float64:
		appendLiteral(out, pythonFloat(v, where))
	case float32:
		appendLiteral(out, pythonFloat(float64(v), where))
	case []byte:
		panic(fmt.Sprintf("%s: []byte is not JSON (send a string, or the bytes with Body)", where))
	default:
		reflected(reflect.ValueOf(value), out, where)
	}
}

func reflected(v reflect.Value, out *[]part, where string) {
	switch v.Kind() {
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		appendLiteral(out, strconv.FormatInt(v.Int(), 10))
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr:
		appendLiteral(out, strconv.FormatUint(v.Uint(), 10))
	case reflect.String:
		appendLiteral(out, quotePython(v.String()))
	case reflect.Bool:
		appendLiteral(out, strconv.FormatBool(v.Bool()))
	case reflect.Float32, reflect.Float64:
		appendLiteral(out, pythonFloat(v.Float(), where))
	case reflect.Pointer, reflect.Interface:
		if v.IsNil() {
			appendLiteral(out, "null")
			return
		}
		jsonParts(v.Elem().Interface(), out, where)
	case reflect.Slice, reflect.Array:
		if v.Kind() == reflect.Slice && v.IsNil() {
			appendLiteral(out, "null")
			return
		}
		appendLiteral(out, "[")
		for i := range v.Len() {
			if i > 0 {
				appendLiteral(out, ",")
			}
			jsonParts(v.Index(i).Interface(), out, fmt.Sprintf("%s[%d]", where, i))
		}
		appendLiteral(out, "]")
	case reflect.Map:
		if v.Type().Key().Kind() != reflect.String {
			panic(fmt.Sprintf("%s: JSON object keys are strings, got %s", where, v.Type().Key()))
		}
		if v.IsNil() {
			appendLiteral(out, "null")
			return
		}
		keys := v.MapKeys()
		slices.SortFunc(keys, func(a, b reflect.Value) int { return strings.Compare(a.String(), b.String()) })
		object := make(Object, len(keys))
		for i, key := range keys {
			object[i] = Field{key.String(), v.MapIndex(key).Interface()}
		}
		jsonParts(object, out, where)
	case reflect.Struct:
		jsonParts(structObject(v), out, where)
	default:
		panic(fmt.Sprintf("%s: %s is not JSON", where, v.Type()))
	}
}

// structObject is a struct's exported fields in order, named and skipped by their `json` tags
// ("-", "name", "name,omitempty"); an embedded struct's fields are inlined.
func structObject(v reflect.Value) Object {
	var object Object
	for i := range v.NumField() {
		field := v.Type().Field(i)
		if !field.IsExported() {
			continue
		}
		name, options, _ := strings.Cut(field.Tag.Get("json"), ",")
		if name == "-" && options == "" {
			continue
		}
		value := v.Field(i)
		if field.Anonymous && name == "" && value.Kind() == reflect.Struct {
			object = append(object, structObject(value)...)
			continue
		}
		if name == "" {
			name = field.Name
		}
		if strings.Contains(","+options+",", ",omitempty,") && value.IsZero() {
			continue
		}
		object = append(object, Field{name, value.Interface()})
	}
	return object
}

// quotePython is s as a JSON string as Python's json.dumps writes it (ensure_ascii): \" \\ \n
// \r \t \b \f, any other character outside space to ~ as \u00xx or a UTF-16 surrogate pair.
func quotePython(s string) string {
	var b strings.Builder
	b.Grow(len(s) + 2)
	b.WriteByte('"')
	for i := 0; i < len(s); {
		c := s[i]
		if c >= ' ' && c <= '~' && c != '"' && c != '\\' {
			b.WriteByte(c)
			i++
			continue
		}
		r, size := utf8.DecodeRuneInString(s[i:])
		i += size
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			if r >= 0x10000 {
				r -= 0x10000
				fmt.Fprintf(&b, `\u%04x\u%04x`, 0xd800|(r>>10), 0xdc00|(r&0x3ff))
			} else {
				fmt.Fprintf(&b, `\u%04x`, r)
			}
		}
	}
	b.WriteByte('"')
	return b.String()
}

// pythonFloat is f as Python's repr writes it: the shortest digits that round-trip, positional
// when the decimal point falls within 16 places of them (1e-05 and 1e+16 are exponential), and a
// ".0" on an integral value. It panics on NaN and infinities, which JSON cannot hold.
func pythonFloat(f float64, where string) string {
	if math.IsNaN(f) || math.IsInf(f, 0) {
		panic(fmt.Sprintf("%s: %v is not JSON", where, f))
	}
	text := strconv.FormatFloat(f, 'e', -1, 64)
	sign := ""
	if text[0] == '-' {
		sign, text = "-", text[1:]
	}
	mantissa, exponent, _ := strings.Cut(text, "e")
	digits := strings.Replace(mantissa, ".", "", 1)
	exp, _ := strconv.Atoi(exponent)
	point := exp + 1 // the decimal point's position after the first digit
	switch {
	case point > -4 && point <= 16:
		switch {
		case point <= 0:
			return sign + "0." + strings.Repeat("0", -point) + digits
		case point >= len(digits):
			return sign + digits + strings.Repeat("0", point-len(digits)) + ".0"
		default:
			return sign + digits[:point] + "." + digits[point:]
		}
	default:
		out := sign + digits[:1]
		if len(digits) > 1 {
			out += "." + digits[1:]
		}
		expSign := "+"
		if exp < 0 {
			expSign, exp = "-", -exp
		}
		return fmt.Sprintf("%se%s%02d", out, expSign, exp)
	}
}
