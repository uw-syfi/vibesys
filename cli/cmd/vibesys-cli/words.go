package main

import (
	"strings"
)

// splitWords splits a command written as one argument, such as --service's, into its argv, the way
// a POSIX shell splits words: blanks separate words; single quotes keep everything up to the next
// single quote; double quotes keep everything up to the next unescaped double quote, where a
// backslash escapes $, `, ", \ and a newline; and a backslash outside quotes keeps the next byte.
// Nothing is expanded. An unterminated quote or a trailing backslash is an error, so a command is
// never run cut short. Every delimiter is ASCII, so it works on bytes and keeps any other byte as it
// is, valid UTF-8 or not.
func splitWords(flagName, s string) ([]string, error) {
	var (
		words   []string
		word    strings.Builder
		inWord  bool
		unended = func(what string) error { return usagef("%s: %s in %q", flagName, what, s) }
	)
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch c {
		case ' ', '\t', '\n':
			if inWord {
				words = append(words, word.String())
				word.Reset()
				inWord = false
			}
		case '\\':
			if i+1 == len(s) {
				return nil, unended("a trailing backslash")
			}
			i++
			word.WriteByte(s[i])
			inWord = true
		case '\'':
			end := strings.IndexByte(s[i+1:], '\'')
			if end < 0 {
				return nil, unended("an unterminated single quote")
			}
			word.WriteString(s[i+1 : i+1+end])
			i, inWord = i+1+end, true
		case '"':
			i++
			for ; i < len(s) && s[i] != '"'; i++ {
				if s[i] == '\\' && i+1 < len(s) && strings.IndexByte("$`\"\\\n", s[i+1]) >= 0 {
					i++
				}
				word.WriteByte(s[i])
			}
			if i == len(s) {
				return nil, unended("an unterminated double quote")
			}
			inWord = true
		default:
			word.WriteByte(c)
			inWord = true
		}
	}
	if inWord {
		words = append(words, word.String())
	}
	return words, nil
}
