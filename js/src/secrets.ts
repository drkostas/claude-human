/** Secrets the app's build wrote into its native resources (the "@drkostas/expo-ntfy/build-secrets" config plugin),
 *  read at run time, so the JavaScript bundle carries none and an over-the-air update of it holds no secret.
 *
 *  import { buildSecret } from "@drkostas/expo-ntfy/secrets";
 *  const token = buildSecret("apiToken");   // undefined on the web, in Node, or when the build wrote none
 */

/** The native module's one call: the secret of that name, or null. */
export type NativeSecrets = { get(name: string): string | null };

let native: NativeSecrets | null | undefined;

/** Use `stand` in place of the native module (a test), or null for none; undefined looks it up again. */
export function setNativeSecrets(stand: NativeSecrets | null | undefined): void {
  native = stand;
}

function load(): NativeSecrets | null {
  if (native === undefined) {
    try {
      // required lazily: expo-modules-core exists only in an Expo app, never in Node or a plain web build
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const core = require("expo-modules-core") as {
        requireOptionalNativeModule<T>(name: string): T | null;
      };
      native = core.requireOptionalNativeModule<NativeSecrets>("ExpoNtfyBuildSecrets");
    } catch {
      native = null;
    }
  }
  return native ?? null;
}

/** The secret the build wrote under `name`, or undefined when there is none (no native module, or no such name). */
export function buildSecret(name: string): string | undefined {
  const value = load()?.get(name);
  return value ? value : undefined;
}
