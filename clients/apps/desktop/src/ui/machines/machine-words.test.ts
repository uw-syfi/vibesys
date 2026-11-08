import {describe, expect, it} from '@effect/vitest';
import {ABSENCE_CAUSES, absenceNote, MACHINE_CHIP_WHYS, machineWhyWord} from './machine-words.js';

describe('the word a chip gives a machine reading that is not current', () => {
  it('names every why but disconnected, with a label, a tone and a hint', () => {
    for (const why of MACHINE_CHIP_WHYS) {
      const word = machineWhyWord(why);
      expect(word.label.length).toBeGreaterThan(0);
      expect(word.hint.length).toBeGreaterThan(0);
      expect(['neutral', 'live', 'ok', 'warn', 'err']).toContain(word.tone);
    }
  });

  it('gives each why its own label, so no two chips read alike', () => {
    const labels = MACHINE_CHIP_WHYS.map(why => machineWhyWord(why).label);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it('writes each hint as a sentence, so a listener hears a whole thought', () => {
    for (const why of MACHINE_CHIP_WHYS) {
      expect(machineWhyWord(why).hint.endsWith('.')).toBe(true);
    }
  });

  it('keeps disconnected out of the chips, for the status chip already speaks for it', () => {
    expect(MACHINE_CHIP_WHYS).not.toContain('disconnected');
  });
});

describe('the note a gauge gives a section the host has no reading of', () => {
  it('names every absence, in lowercase, with its own words', () => {
    const notes = ABSENCE_CAUSES.map(absenceNote);
    for (const note of notes) {
      expect(note.length).toBeGreaterThan(0);
      expect(note).toBe(note.toLowerCase());
    }
    expect(new Set(notes).size).toBe(notes.length);
  });
});
