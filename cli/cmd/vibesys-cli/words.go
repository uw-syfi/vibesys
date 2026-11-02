package main

import (
	"strings"
)

// splitWords splits a command written as one argument, such as --service's, into its argv, the way
// a POSIX shell splits words: blanks separate words; single quotes keep everything up to the next
// single quote; double quotes keep everything up to the next unescaped double quote, where a
// backslash escapes $, `, ", \ and a newline; and a backslash outside quotes keeps the next
// character. Nothing is expanded. An unterminated quote or a trailing backslash is an error, so a
// command is never run cut short.
func splitWords(flagName, s string) ([]string, error) {
	var (
		words   []string
		word    strings.Builder
		inWord  bool
		runes   = []rune(s)
		unended = func(what string) error { return usagef("%s: %s in %q", flagName, what, s) }
	)
	for i := 0; i < len(runes); i++ {
		r := runes[i]
		switch {
		case r == ' ' || r == '\t' || r == '\n':
			if inWord {
				words = append(words, word.String())
				word.Reset()
				inWord = false
			}
		case r == '\\':
			if i+1 == len(runes) {
				return nil, unended("a trailing backslash")
			}
			i++
			word.WriteRune(runes[i])
			inWord = true
		case r == '\'':
			end := indexRune(runes, i+1, '\'')
			if end < 0 {
				return nil, unended("an unterminated single quote")
			}
			word.WriteString(string(runes[i+1 : end]))
			i, inWord = end, true
		case r == '"':
			i++
			for ; i < len(runes) && runes[i] != '"'; i++ {
				if runes[i] == '\\' && i+1 < len(runes) && strings.ContainsRune("$`\"\\\n", runes[i+1]) {
					i++
				}
				word.WriteRune(runes[i])
			}
			if i == len(runes) {
				return nil, unended("an unterminated double quote")
			}
			inWord = true
		default:
			word.WriteRune(r)
			inWord = true
		}
	}
	if inWord {
		words = append(words, word.String())
	}
	return words, nil
}

func indexRune(runes []rune, from int, r rune) int {
	for i := from; i < len(runes); i++ {
		if runes[i] == r {
			return i
		}
	}
	return -1
}
