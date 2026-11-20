/**
 * Expo config plugin that puts secrets known at build time into the app's native resources, where the JavaScript
 * reads them at run time with `buildSecret(name)` from "@drkostas/expo-ntfy/secrets".
 *
 * An app that reads a token from `process.env.EXPO_PUBLIC_...` has that token inlined into its JavaScript bundle.
 * An over-the-air update of the JavaScript (expo-updates) is then a file that carries the secret, served to anyone
 * who can fetch it, and it must carry the token of the build on the phone. With this plugin the bundle carries no
 * secret: the build writes each one into android/app/src/main/res/values/build_secrets.xml at prebuild, a native
 * module of this package reads it by name, and an update of the JavaScript keeps reading the installed build's own.
 *
 * The secrets are part of the APK, as a BuildConfig field would be, so give each one only what that build needs.
 * The plugin's options name environment variables, never values, so the app config (which an exported update
 * carries as its manifest's expoClient) holds no secret either. A variable that is unset or empty fails the prebuild,
 * and it is read only at prebuild, so reading the app config (expo start, a web build) never needs it.
 *
 * Options:
 *   secrets   { <name>: <environment variable> }, each name a plain name (a letter, then letters, digits or _)
 *
 *   ["@drkostas/expo-ntfy/build-secrets", { "secrets": { "apiToken": "MY_APP_API_TOKEN" } }]
 *   // in the app: import { buildSecret } from "@drkostas/expo-ntfy/secrets"; buildSecret("apiToken")
 *
 * The resource file says tools:keep for its strings, because they are read by name, which a resource shrinker
 * cannot see.
 */
const fs = require("fs");
const path = require("path");

const NAME = /^[A-Za-z][A-Za-z0-9_]*$/;
const ENV = /^[A-Za-z_][A-Za-z0-9_]*$/;
const PREFIX = "build_secret_";
const FILE = "build_secrets.xml";

function loadConfigPlugins(config) {
  const roots = [config && config._internal && config._internal.projectRoot, process.cwd(), __dirname]
    .filter(Boolean);
  for (const name of ["expo/config-plugins", "@expo/config-plugins"]) {
    try {
      return require(require.resolve(name, { paths: roots }));
    } catch (e) {
      // try the next one
    }
  }
  throw new Error("expo-ntfy: no @expo/config-plugins found");
}

/** The secrets to write, [{name, value}], from the options and the environment. Throws on a bad name, a bad
 *  variable name, and a variable that is unset or empty. */
function resolveSecrets(options = {}, env = process.env) {
  const secrets = options.secrets || {};
  if (typeof secrets !== "object" || Array.isArray(secrets)) {
    throw new Error('expo-ntfy build-secrets: "secrets" maps each name to an environment variable');
  }
  return Object.entries(secrets).map(([name, variable]) => {
    if (!NAME.test(name)) {
      throw new Error(`expo-ntfy build-secrets: ${JSON.stringify(name)} is not a plain name (a letter, then letters, digits or _)`);
    }
    if (typeof variable !== "string" || !ENV.test(variable)) {
      throw new Error(`expo-ntfy build-secrets: the secret ${name} names no environment variable: ${JSON.stringify(variable)}`);
    }
    const value = env[variable];
    if (!value) {
      throw new Error(`expo-ntfy build-secrets: ${variable} is unset or empty, so the secret ${name} would be missing from the build`);
    }
    return { name, value };
  });
}

/** A string resource's text for `value`: XML's five characters escaped, and Android's own (backslash, both quotes,
 *  and a leading @ or ?) escaped with a backslash, so getString gives `value` back unchanged. */
function resourceText(value) {
  let s = String(value).replace(/\\/g, "\\\\").replace(/'/g, "\\'").replace(/"/g, '\\"');
  if (/^[@?]/.test(s)) s = "\\" + s;
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** The resource file for the secrets. */
function renderResources(secrets) {
  const lines = secrets.map(
    ({ name, value }) => `  <string name="${PREFIX}${name}" translatable="false">${resourceText(value)}</string>`,
  );
  return [
    '<?xml version="1.0" encoding="utf-8"?>',
    "<!-- Written by @drkostas/expo-ntfy/build-secrets at prebuild. It holds secrets: never commit or edit it. -->",
    `<resources xmlns:tools="http://schemas.android.com/tools" tools:keep="@string/${PREFIX}*">`,
    ...lines,
    "</resources>",
    "",
  ].join("\n");
}

function withBuildSecrets(config, options = {}) {
  const { withDangerousMod } = loadConfigPlugins(config);
  return withDangerousMod(config, [
    "android",
    (cfg) => {
      const secrets = resolveSecrets(options);
      const dir = path.join(cfg.modRequest.platformProjectRoot, "app/src/main/res/values");
      fs.mkdirSync(dir, { recursive: true });
      const file = path.join(dir, FILE);
      fs.writeFileSync(file, renderResources(secrets), { mode: 0o600 });
      // the mode above applies only to a file it creates, and a prebuild without --clean finds this one there
      fs.chmodSync(file, 0o600);
      return cfg;
    },
  ]);
}

module.exports = withBuildSecrets;
module.exports.withBuildSecrets = withBuildSecrets;
module.exports.resolveSecrets = resolveSecrets;
module.exports.resourceText = resourceText;
module.exports.renderResources = renderResources;
module.exports.PREFIX = PREFIX;
module.exports.FILE = FILE;
