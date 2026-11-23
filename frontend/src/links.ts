/**
 * Where the source of this console lives, linked on the sign-in screen and in the console header
 * so a reviewer can read the code before or while trying the demo. A fork sets its own with
 * VITE_SOURCE_URL at build time; an empty value hides both links.
 */
const fromEnv = import.meta.env.VITE_SOURCE_URL as string | undefined;

export const SOURCE_URL: string =
  fromEnv !== undefined ? fromEnv.trim() : "https://github.com/jefersonbraga/regulated-agent-lab";
