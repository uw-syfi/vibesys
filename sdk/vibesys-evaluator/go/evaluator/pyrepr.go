package evaluator

import (
	"math"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf16"
	"unicode/utf8"
)

// The records are byte-compatible with the ones the Python implementation wrote: the same key
// order, separators, number text and ASCII-only string escapes (Python's json.dumps with
// ensure_ascii and separators (",", ":")), and the same messages (Python's repr of strings,
// floats and lists).

// pyFloat is Python's repr of a float.
func pyFloat(f float64) string {
	switch {
	case math.IsNaN(f):
		return "nan"
	case math.IsInf(f, 1):
		return "inf"
	case math.IsInf(f, -1):
		return "-inf"
	}
	if f == 0 {
		if math.Signbit(f) {
			return "-0.0"
		}
		return "0.0"
	}
	// The shortest digits that round-trip, then Python's layout rule: scientific when the
	// decimal exponent is below -4 or at least 16.
	e := strconv.FormatFloat(f, 'e', -1, 64) // d.ddde+XX
	mant, expText, _ := strings.Cut(e, "e")
	exp, _ := strconv.Atoi(expText)
	neg := strings.HasPrefix(mant, "-")
	digits := strings.ReplaceAll(strings.TrimPrefix(mant, "-"), ".", "")
	var out string
	switch {
	case exp < -4 || exp >= 16:
		out = digits[:1]
		if len(digits) > 1 {
			out += "." + digits[1:]
		}
		sign := "+"
		if exp < 0 {
			sign = "-"
			exp = -exp
		}
		out += "e" + sign + pad2(exp)
	case exp < 0:
		out = "0." + strings.Repeat("0", -exp-1) + digits
	case exp+1 >= len(digits):
		out = digits + strings.Repeat("0", exp+1-len(digits)) + ".0"
	default:
		out = digits[:exp+1] + "." + digits[exp+1:]
	}
	if neg {
		return "-" + out
	}
	return out
}

func pad2(n int) string {
	if n < 10 {
		return "0" + strconv.Itoa(n)
	}
	return strconv.Itoa(n)
}

// pyRepr is Python's repr of a str.
func pyRepr(s string) string {
	quote := byte('\'')
	if strings.Contains(s, "'") && !strings.Contains(s, `"`) {
		quote = '"'
	}
	var b strings.Builder
	b.WriteByte(quote)
	for _, r := range s {
		switch {
		case r == rune(quote) || r == '\\':
			b.WriteByte('\\')
			b.WriteRune(r)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r < 0x20 || r == 0x7f:
			b.WriteString(`\x` + hex2(int(r)))
		case r == utf8.RuneError || !unicode.IsPrint(r):
			switch {
			case r < 0x100:
				b.WriteString(`\x` + hex2(int(r)))
			case r < 0x10000:
				b.WriteString(`\u` + hex4(int(r)))
			default:
				b.WriteString(`\U` + hex4(int(r)>>16) + hex4(int(r)&0xffff))
			}
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte(quote)
	return b.String()
}

// pyList is Python's repr of a list of str.
func pyList(items []string) string {
	parts := make([]string, len(items))
	for i, item := range items {
		parts[i] = pyRepr(item)
	}
	return "[" + strings.Join(parts, ", ") + "]"
}

func hex2(n int) string {
	const digits = "0123456789abcdef"
	return string([]byte{digits[n>>4&15], digits[n&15]})
}

func hex4(n int) string { return hex2(n>>8&255) + hex2(n&255) }

// jsonString is Python's json.dumps of a str (ensure_ascii).
func jsonString(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch {
		case r == '"':
			b.WriteString(`\"`)
		case r == '\\':
			b.WriteString(`\\`)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r == '\b':
			b.WriteString(`\b`)
		case r == '\f':
			b.WriteString(`\f`)
		case r < 0x20:
			b.WriteString(`\u` + hex4(int(r)))
		case r < 0x7f:
			b.WriteRune(r)
		case r < 0x10000:
			b.WriteString(`\u` + hex4(int(r)))
		default:
			hi, lo := utf16.EncodeRune(r)
			b.WriteString(`\u` + hex4(int(hi)) + `\u` + hex4(int(lo)))
		}
	}
	b.WriteByte('"')
	return b.String()
}
