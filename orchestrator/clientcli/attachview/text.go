package attachview

import (
	"strings"
	"unicode"
	"unicode/utf8"
)

// Sanitize makes text from an agent, a tool or a user safe to print on a terminal: it removes every
// terminal control sequence (CSI, OSC, DCS and the other escape sequences), the control characters
// that remain, the characters that reorder text on screen, and bytes that are not UTF-8. Line breaks
// stay; a tab becomes a space and a carriage return ends a line, so a progress bar cannot overwrite
// what was printed before it. The text is untrusted, and this is the one place it is made safe.
func Sanitize(text string) string {
	if text == "" {
		return ""
	}
	text = strings.ToValidUTF8(text, "�")
	var b strings.Builder
	b.Grow(len(text))
	runes := []rune(text)
	for i := 0; i < len(runes); i++ {
		r := runes[i]
		switch {
		case r == 0x1b:
			i = skipEscape(runes, i)
		case r == '\n':
			b.WriteRune(r)
		case r == '\r':
			if i+1 < len(runes) && runes[i+1] == '\n' {
				continue
			}
			b.WriteByte('\n')
		case r == '\t':
			b.WriteByte(' ')
		case unicode.IsControl(r), isReordering(r), r == 0xFEFF:
		default:
			b.WriteRune(r)
		}
	}
	return b.String()
}

// isReordering says the character changes the order text is drawn in, which could make a line read
// as something other than what it says.
func isReordering(r rune) bool {
	return (r >= 0x202A && r <= 0x202E) || (r >= 0x2066 && r <= 0x2069) || r == 0x200E || r == 0x200F || r == 0x061C
}

// skipEscape returns the index of the last rune of the escape sequence that starts at runes[i].
func skipEscape(runes []rune, i int) int {
	if i+1 >= len(runes) {
		return i
	}
	switch runes[i+1] {
	case '[': // CSI: parameters, intermediates, then a final byte 0x40-0x7E.
		for j := i + 2; j < len(runes); j++ {
			if runes[j] >= 0x40 && runes[j] <= 0x7e {
				return j
			}
			if runes[j] < 0x20 || runes[j] > 0x7e {
				return j - 1
			}
		}
		return len(runes) - 1
	case ']', 'P', 'X', '^', '_': // OSC, DCS, SOS, PM, APC: up to BEL or the string terminator ESC \.
		for j := i + 2; j < len(runes); j++ {
			if runes[j] == 0x07 {
				return j
			}
			if runes[j] == 0x1b {
				if j+1 < len(runes) && runes[j+1] == '\\' {
					return j + 1
				}
				return j - 1
			}
		}
		return len(runes) - 1
	}
	// A two-character escape, with any intermediates (0x20-0x2F) before its final byte.
	j := i + 1
	for j < len(runes) && runes[j] >= 0x20 && runes[j] <= 0x2f {
		j++
	}
	if j < len(runes) && runes[j] >= 0x30 && runes[j] <= 0x7e {
		return j
	}
	return j - 1
}

// Clip cuts text to at most width runes, ending in "..." when it cut. A width below 4 only counts
// runes, so a very narrow terminal still gets something. A width of zero or less cuts nothing.
func Clip(text string, width int) string {
	if width <= 0 || utf8.RuneCountInString(text) <= width {
		return text
	}
	runes := []rune(text)
	if width <= 3 {
		return string(runes[:width])
	}
	return string(runes[:width-3]) + "..."
}

// firstLine is the first line of text that is not blank, sanitized, with a mark when more follows.
func firstLine(text string) string {
	lines := strings.Split(Sanitize(text), "\n")
	for i, line := range lines {
		if line = strings.TrimSpace(line); line != "" {
			if rest := strings.TrimSpace(strings.Join(lines[i+1:], "")); rest != "" {
				return line + " ..."
			}
			return line
		}
	}
	return ""
}

// lastLine is the last line of text that is not blank, sanitized.
func lastLine(text string) string {
	lines := strings.Split(Sanitize(text), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if line := strings.TrimSpace(lines[i]); line != "" {
			return line
		}
	}
	return ""
}
