import type {JSX} from 'react';
import {
  siAmd,
  siAnthropic,
  siApple,
  siClaudecode,
  siDeepseek,
  siGoogle,
  siGooglegemini,
  siMeta,
  siMoonshotai,
  siNvidia,
  siOpencode,
} from 'simple-icons';

/**
 * The marks the app draws, from the `simple-icons` package (CC0-1.0, pinned to an exact version).
 * The package's license covers its files, not the brands: a mark here is a trademark of its owner
 * and appears only beside the product's name, to say which product a choice is, never as a sign
 * that the owner endorses VibeSys. Every path is the package's own, never redrawn or approximated:
 * a maker, vendor or product the package has no mark for is shown by name alone (a harness, by the
 * generic glyph).
 */
const MARKS = {
  amd: siAmd.path,
  anthropic: siAnthropic.path,
  apple: siApple.path,
  claudecode: siClaudecode.path,
  deepseek: siDeepseek.path,
  google: siGoogle.path,
  googlegemini: siGooglegemini.path,
  meta: siMeta.path,
  moonshotai: siMoonshotai.path,
  nvidia: siNvidia.path,
  opencode: siOpencode.path,
} as const;

export type BrandMarkName = keyof typeof MARKS;

/** The names of the marks the app can draw. */
export const BRAND_MARK_NAMES = Object.keys(MARKS) as readonly BrandMarkName[];

/**
 * A product's mark in the text color, so it is right in every theme, beside the product's name
 * (the mark is decoration: the name is its label). With no `mark`, a neutral glyph of a
 * terminal prompt.
 */
export function BrandMark(props: {readonly mark: BrandMarkName | null}): JSX.Element {
  return (
    <svg
      className="vs-brand-mark"
      viewBox="0 0 24 24"
      width="24"
      height="24"
      aria-hidden="true"
      focusable="false"
      data-generic={props.mark === null || undefined}
    >
      {props.mark === null ? (
        <>
          <rect x="3" y="4" width="18" height="16" rx="3" />
          <path d="M7.5 9.5 11 12l-3.5 2.5M12.5 15h4" />
        </>
      ) : (
        <path d={MARKS[props.mark]} />
      )}
    </svg>
  );
}
