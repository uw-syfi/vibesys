/**
 * The words of the project question that are not the user's or the machine's: a folder that is a
 * project among the completions, the button that takes the typed folder, and, when the project's
 * tasks could not be read, the way back to another project and why the form waits. Pure; the pages
 * only lay them out.
 */

/** What a completion is when its folder is a VibeSys project. */
export const PROJECT_FOLDER_WORDS = 'VibeSys project';

/** The folder field's button, which makes the typed folder the project. */
export const USE_FOLDER_WORDS = 'Use this folder';

/** The way back to the project question from a project whose tasks could not be read. */
export const OTHER_PROJECT_WORDS = 'Choose another project';

/** Why the project question cannot go on while its tasks could not be read. */
export const TASKS_UNREAD_WORDS = 'Choose a project whose tasks can be read.';
