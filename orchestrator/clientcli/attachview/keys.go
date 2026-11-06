package attachview

import "unicode/utf8"

// KeyKind says which key.
type KeyKind int

// The keys the view reads.
const (
	KeyRune KeyKind = iota
	KeyEsc
	KeyEnter
	KeyBackspace
	KeyCtrlC
)

// Key is one key press. Rune is set for KeyRune.
type Key struct {
	Kind KeyKind
	Rune rune
}

// ParseKeys reads the keys in bytes the terminal delivered at once. A lone escape is the Esc key;
// an escape followed by more is a key such as an arrow, which the view has no use for and drops.
func ParseKeys(b []byte) []Key {
	var keys []Key
	for i := 0; i < len(b); {
		switch c := b[i]; {
		case c == 0x1b:
			if i+1 == len(b) {
				keys = append(keys, Key{Kind: KeyEsc})
				i++
				break
			}
			i = skipKeySequence(b, i)
		case c == 0x03:
			keys = append(keys, Key{Kind: KeyCtrlC})
			i++
		case c == '\r' || c == '\n':
			keys = append(keys, Key{Kind: KeyEnter})
			i++
		case c == 0x7f || c == 0x08:
			keys = append(keys, Key{Kind: KeyBackspace})
			i++
		case c < 0x20:
			i++
		default:
			r, size := utf8.DecodeRune(b[i:])
			if r != utf8.RuneError {
				keys = append(keys, Key{Kind: KeyRune, Rune: r})
			}
			i += size
		}
	}
	return keys
}

// skipKeySequence returns the index after the escape sequence that starts at b[i].
func skipKeySequence(b []byte, i int) int {
	switch b[i+1] {
	case '[':
		for j := i + 2; j < len(b); j++ {
			if b[j] >= 0x40 && b[j] <= 0x7e {
				return j + 1
			}
		}
		return len(b)
	case 'O':
		return min(i+3, len(b))
	}
	return i + 2
}
