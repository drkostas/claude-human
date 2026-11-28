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
 *   secrets                { <name>: <environment variable> }, each name a plain name (a letter, then letters, digits
 *                          or _)
 *   updatesRequestHeaders  { <header>: <template> }, request headers expo-updates sends with every request for an
 *                          update, each template naming one or more environment variables as ${NAME}
 *
 *   ["@drkostas/expo-ntfy/build-secrets", { "secrets": { "apiToken": "MY_APP_API_TOKEN" } }]
 *   // in the app: import { buildSecret } from "@drkostas/expo-ntfy/secrets"; buildSecret("apiToken")
 *
 *   ["@drkostas/expo-ntfy/build-secrets", { "updatesRequestHeaders": { "Authorization": "Bearer ${MY_UPDATE_TOKEN}" } }]
 *
 * An update server that wants a token from the app gets it from expo-updates' request headers. Those come from
 * updates.requestHeaders in the app config, which an exported update carries and which the runtime version's
 * fingerprint reads, so a token there is published with every update and gives every build its own runtime. They can
 * be replaced at run time with Updates.setUpdateRequestHeadersOverride, but expo-updates 57 keeps that override and
 * from then on launches only the updates it fetched with the same headers. The update embedded in the APK keeps the
 * build's own headers, so after the first start it never qualifies, and every later start is an emergency launch
 * of the embedded code. updatesRequestHeaders writes the header into the Android manifest's meta-data
 * (expo.modules.updates.UPDATES_CONFIGURATION_REQUEST_HEADERS_KEY), which is what expo-updates reads as the build's
 * own headers. Every update then launches with the same headers as the embedded one, and the app config holds none.
 * Declare the header in updates.requestHeaders with a stand-in value, so the app config says the header exists. The
 * manifest's value replaces the stand-in, and keeps any other header declared there. The manifest is written in a
 * finalized step, after expo-updates' own step has written the app config's headers, so the order of the plugins
 * does not matter. Android only.
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
/** the meta-data expo-updates reads its build's own request headers from, a JSON object of header to value */
const UPDATES_HEADERS_KEY = "expo.modules.updates.UPDATES_CONFIGURATION_REQUEST_HEADERS_KEY";
/** an HTTP field name, RFC 9110's token */
const HEADER = /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/;
const PLACEHOLDER = /\$\{([^}]*)\}/g;

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

/** The request headers to give expo-updates, {header: value}, from the options' templates and the environment. Throws
 *  on a bad header name, a template that is not a string or names no variable, a bad variable name, and a variable that
 *  is unset or empty. */
function resolveUpdatesHeaders(options = {}, env = process.env) {
  const headers = options.updatesRequestHeaders;
  if (headers === undefined) return {};
  if (!headers || typeof headers !== "object" || Array.isArray(headers)) {
    throw new Error('expo-ntfy build-secrets: "updatesRequestHeaders" maps each header to a template such as "Bearer ${MY_TOKEN}"');
  }
  const out = {};
  for (const [header, template] of Object.entries(headers)) {
    if (!HEADER.test(header)) {
      throw new Error(`expo-ntfy build-secrets: ${JSON.stringify(header)} is not a header name`);
    }
    if (typeof template !== "string") {
      throw new Error(`expo-ntfy build-secrets: the header ${header} has no template: ${JSON.stringify(template)}`);
    }
    const names = [...template.matchAll(PLACEHOLDER)].map((m) => m[1]);
    if (!names.length) {
      throw new Error(`expo-ntfy build-secrets: the header ${header} names no environment variable. Write it as a template such as "Bearer \${MY_TOKEN}", or put a value that is no secret in updates.requestHeaders`);
    }
    for (const name of names) {
      if (!ENV.test(name)) {
        throw new Error(`expo-ntfy build-secrets: the header ${header} names no environment variable: ${JSON.stringify("${" + name + "}")}`);
      }
      if (!env[name]) {
        throw new Error(`expo-ntfy build-secrets: ${name} is unset or empty, so the header ${header} would be missing from the build`);
      }
    }
    out[header] = template.replace(PLACEHOLDER, (_, name) => env[name]);
  }
  return out;
}

/** The headers expo-updates reads from `declared` (the manifest's JSON, or null) with `headers` written over them. A
 *  declared header of the same name in another case is replaced, since expo-updates compares names without case. */
function mergeUpdatesHeaders(declared, headers) {
  let base = {};
  if (declared) {
    try {
      base = JSON.parse(declared);
    } catch (e) {
      throw new Error(`expo-ntfy build-secrets: the manifest's ${UPDATES_HEADERS_KEY} is not JSON`);
    }
  }
  const ours = new Set(Object.keys(headers).map((h) => h.toLowerCase()));
  const kept = Object.fromEntries(Object.entries(base).filter(([h]) => !ours.has(h.toLowerCase())));
  return { ...kept, ...headers };
}

/** Write `headers` into the Android manifest at `file`, over the request headers expo-updates' own step wrote there. */
async function writeUpdatesHeaders(file, headers, Manifest) {
  const manifest = await Manifest.readAndroidManifestAsync(file);
  const app = Manifest.getMainApplicationOrThrow(manifest);
  const declared = Manifest.getMainApplicationMetaDataValue(manifest, UPDATES_HEADERS_KEY);
  Manifest.addMetaDataItemToMainApplication(app, UPDATES_HEADERS_KEY, JSON.stringify(mergeUpdatesHeaders(declared, headers)));
  await Manifest.writeAndroidManifestAsync(file, manifest);
  // the manifest now holds a secret, as build_secrets.xml does
  fs.chmodSync(file, 0o600);
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
  const { withDangerousMod, withFinalizedMod, AndroidConfig } = loadConfigPlugins(config);
  if (options.updatesRequestHeaders !== undefined) {
    config = withFinalizedMod(config, [
      "android",
      async (cfg) => {
        const headers = resolveUpdatesHeaders(options);
        const file = path.join(cfg.modRequest.platformProjectRoot, "app/src/main/AndroidManifest.xml");
        await writeUpdatesHeaders(file, headers, AndroidConfig.Manifest);
        return cfg;
      },
    ]);
  }
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
module.exports.resolveUpdatesHeaders = resolveUpdatesHeaders;
module.exports.mergeUpdatesHeaders = mergeUpdatesHeaders;
module.exports.UPDATES_HEADERS_KEY = UPDATES_HEADERS_KEY;
module.exports.PREFIX = PREFIX;
module.exports.FILE = FILE;
