package main

import (
	"slices"
	"strings"
	"testing"
	"testing/quick"
)

// quoteWord is how a shell user writes any word so that it reaches the command as it is.
func quoteWord(w string) string { return "'" + strings.ReplaceAll(w, "'", `'\''`) + "'" }

// Any argv, written as a shell user quotes it, splits back into itself: empty words, blanks,
// quotes, backslashes and bytes that are not UTF-8 included.
func TestAQuotedArgvSplitsBackIntoItself(t *testing.T) {
	property := func(argv []string) bool {
		quoted := make([]string, len(argv))
		for i, w := range argv {
			quoted[i] = quoteWord(w)
		}
		got, err := splitWords("--service", strings.Join(quoted, " \t"))
		return err == nil && slices.Equal(got, argv) || (len(argv) == 0 && err == nil && len(got) == 0)
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 2000}); err != nil {
		t.Fatal(err)
	}
}

func TestSplitWordsReadsQuotesAsAShellDoes(t *testing.T) {
	cases := map[string][]string{
		`server --port 8080`:          {"server", "--port", "8080"},
		`sh -c 'echo "a b"; exit 0'`:  {"sh", "-c", `echo "a b"; exit 0`},
		`print "a \"quoted\" \$HOME"`: {"print", `a "quoted" $HOME`},
		`"keep \n as is"`:             {`keep \n as is`},
		`a\ b c`:                      {"a b", "c"},
		`'' x`:                        {"", "x"},
		`  spaced   out  `:            {"spaced", "out"},
		`ab'cd'"ef"`:                  {"abcdef"},
		``:                            nil,
	}
	for in, want := range cases {
		if got, err := splitWords("--service", in); err != nil || !slices.Equal(got, want) {
			t.Errorf("splitWords(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	for _, bad := range []string{`'open`, `"open`, `trailing\`, `a "b\"`} {
		if got, err := splitWords("--probe", bad); err == nil || !strings.Contains(err.Error(), "--probe") {
			t.Errorf("splitWords(%q) = %q, %v; want an error naming the flag", bad, got, err)
		}
	}
}

// Whatever it is given, splitWords refuses it naming the flag, or returns words that, quoted again,
// split into the same words.
func FuzzSplitWords(f *testing.F) {
	for _, s := range []string{`a b`, `'a b' "c\"d" e\ f`, `'`, `"\`, "\xff 'x\x00'"} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, s string) {
		words, err := splitWords("--service", s)
		if err != nil {
			if !strings.HasPrefix(err.Error(), "--service: ") {
				t.Fatalf("an error that does not name the flag: %v", err)
			}
			return
		}
		quoted := make([]string, len(words))
		for i, w := range words {
			quoted[i] = quoteWord(w)
		}
		again, err := splitWords("--service", strings.Join(quoted, " "))
		if err != nil || !slices.Equal(again, words) {
			t.Fatalf("%q split into %q, which quoted again splits into %q, %v", s, words, again, err)
		}
	})
}
