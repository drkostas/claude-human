import { chmodSync, cpSync, mkdirSync, mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { buildSecret, setNativeSecrets } from "../src/secrets";

const require = createRequire(import.meta.url);
const withBuildSecrets = require("../plugin/withBuildSecrets.js");
const { compileModsAsync } = require("@expo/config-plugins");

const FIXTURE = join(__dirname, "fixture");
const BASE = { name: "Example", slug: "example", android: { package: "org.example.app" } };
// made of plain words at run time, so the file holds no literal that reads as a real secret
const TOKEN = ["example", "token", "for", "tests"].join("-").repeat(2);

async function prebuild(options: object, env: Record<string, string | undefined> = {},
                        before?: (root: string) => void) {
  const root = mkdtempSync(join(tmpdir(), "expo-ntfy-secrets-"));
  cpSync(FIXTURE, root, { recursive: true });
  before?.(root);
  const saved = { ...process.env };
  Object.assign(process.env, env);
  let config;
  try {
    config = withBuildSecrets({ ...BASE, _internal: { projectRoot: root } }, options);
    await compileModsAsync(config, { projectRoot: root, platforms: ["android"], assertMissingModProviders: false });
  } finally {
    process.env = saved;
  }
  const file = join(root, "android/app/src/main/res/values/build_secrets.xml");
  return { config, file, xml: readFileSync(file, "utf8") };
}

describe("the build-secrets config plugin", () => {
  it("writes each secret into a resource file of its own, kept from the shrinker", async () => {
    const out = await prebuild({ secrets: { apiToken: "EXAMPLE_API_TOKEN" } }, { EXAMPLE_API_TOKEN: TOKEN });
    expect(out.xml).toContain(`<string name="build_secret_apiToken" translatable="false">${TOKEN}</string>`);
    expect(out.xml).toContain('tools:keep="@string/build_secret_*"');
    expect(statSync(out.file).mode & 0o777).toBe(0o600);
  });

  it("makes a file already there 0600 too, as a prebuild without --clean finds it", async () => {
    const out = await prebuild({ secrets: { apiToken: "EXAMPLE_API_TOKEN" } }, { EXAMPLE_API_TOKEN: TOKEN }, (root) => {
      const dir = join(root, "android/app/src/main/res/values");
      mkdirSync(dir, { recursive: true });
      writeFileSync(join(dir, "build_secrets.xml"), "<resources/>\n");
      chmodSync(join(dir, "build_secrets.xml"), 0o644);
    });
    expect(out.xml).toContain(TOKEN);
    expect(statSync(out.file).mode & 0o777).toBe(0o600);
  });

  it("puts no secret into the app config, which an exported update carries", async () => {
    const out = await prebuild({ secrets: { apiToken: "EXAMPLE_API_TOKEN" } }, { EXAMPLE_API_TOKEN: TOKEN });
    expect(JSON.stringify(out.config)).not.toContain(TOKEN);
  });

  it("fails the prebuild on an unset or empty variable, naming it", async () => {
    await expect(prebuild({ secrets: { apiToken: "EXAMPLE_API_TOKEN" } }, { EXAMPLE_API_TOKEN: "" }))
      .rejects.toThrow(/EXAMPLE_API_TOKEN is unset or empty/);
  });

  it("refuses a name that is not a plain name, and a variable that is not one", () => {
    expect(() => withBuildSecrets.resolveSecrets({ secrets: { "api-token": "X" } }, { X: "v" }))
      .toThrow(/is not a plain name/);
    expect(() => withBuildSecrets.resolveSecrets({ secrets: { apiToken: "not a variable" } }, {}))
      .toThrow(/names no environment variable/);
    expect(withBuildSecrets.resolveSecrets({}, {})).toEqual([]);
  });

  it("escapes a value so the resource gives it back unchanged", () => {
    expect(withBuildSecrets.resourceText(`a&b<c>"d'e\\f`)).toBe(`a&amp;b&lt;c&gt;\\"d\\'e\\\\f`);
    expect(withBuildSecrets.resourceText("@string/x")).toBe("\\@string/x");
    expect(withBuildSecrets.resourceText("?attr")).toBe("\\?attr");
  });
});

const { AndroidConfig, withPlugins } = require("@expo/config-plugins");
const HEADERS_KEY = "expo.modules.updates.UPDATES_CONFIGURATION_REQUEST_HEADERS_KEY";
const UPDATES = {
  url: "https://example.org/updates/manifest",
  requestHeaders: { Authorization: "Bearer stand-in", "X-Channel": "main" },
};

/** A prebuild of the fixture with this plugin and expo-updates' own Android step, in the order `order` gives, and the
 *  request headers the manifest ends with. */
async function prebuildHeaders(options: object, env: Record<string, string | undefined>,
                               order: "updates-last" | "updates-first" = "updates-last") {
  const root = mkdtempSync(join(tmpdir(), "expo-ntfy-headers-"));
  cpSync(FIXTURE, root, { recursive: true });
  // expo-updates' step also writes these two, which the fixture does not have
  writeFileSync(join(root, "android/gradle.properties"), "");
  mkdirSync(join(root, "android/app/src/main/res/values"), { recursive: true });
  writeFileSync(join(root, "android/app/src/main/res/values/strings.xml"), "<resources/>\n");
  const saved = { ...process.env };
  Object.assign(process.env, env);
  let config;
  try {
    const ours = [withBuildSecrets, options];
    const updates = AndroidConfig.Updates.withUpdates;
    const base = { ...BASE, runtimeVersion: "1.0.0", updates: UPDATES, _internal: { projectRoot: root } };
    // a prebuild applies expo-updates' plugin after the app's own plugins, so "updates-last" is the usual order
    config = withPlugins(base, order === "updates-last" ? [ours, updates] : [updates, ours]);
    await compileModsAsync(config, { projectRoot: root, platforms: ["android"], assertMissingModProviders: false });
  } finally {
    process.env = saved;
  }
  const file = join(root, "android/app/src/main/AndroidManifest.xml");
  const xml = readFileSync(file, "utf8");
  const manifest = await AndroidConfig.Manifest.readAndroidManifestAsync(file);
  const value = AndroidConfig.Manifest.getMainApplicationMetaDataValue(manifest, HEADERS_KEY);
  return { config, file, xml, headers: value === null ? null : JSON.parse(value) };
}

describe("the build-secrets plugin's updatesRequestHeaders", () => {
  const OPTIONS = { updatesRequestHeaders: { Authorization: "Bearer ${EXAMPLE_UPDATE_TOKEN}" } };

  it("writes the header into the manifest over the app config's stand-in, in either order of the plugins", async () => {
    for (const order of ["updates-last", "updates-first"] as const) {
      const out = await prebuildHeaders(OPTIONS, { EXAMPLE_UPDATE_TOKEN: TOKEN }, order);
      expect(out.headers).toEqual({ Authorization: `Bearer ${TOKEN}`, "X-Channel": "main" });
      expect(out.xml).not.toContain("stand-in");
    }
  });

  it("leaves expo-updates' own headers as they are without the option", async () => {
    const out = await prebuildHeaders({}, {});
    expect(out.headers).toEqual(UPDATES.requestHeaders);
  });

  it("puts no header value into the app config, which an exported update carries and the fingerprint reads", async () => {
    const out = await prebuildHeaders(OPTIONS, { EXAMPLE_UPDATE_TOKEN: TOKEN });
    expect(JSON.stringify(out.config)).not.toContain(TOKEN);
    expect(out.config.updates.requestHeaders).toEqual(UPDATES.requestHeaders);
  });

  it("makes the manifest 0600, as it now holds a secret", async () => {
    const out = await prebuildHeaders(OPTIONS, { EXAMPLE_UPDATE_TOKEN: TOKEN });
    expect(statSync(out.file).mode & 0o777).toBe(0o600);
  });

  it("fails the prebuild on an unset or empty variable, naming it and the header", async () => {
    await expect(prebuildHeaders(OPTIONS, { EXAMPLE_UPDATE_TOKEN: "" }))
      .rejects.toThrow(/EXAMPLE_UPDATE_TOKEN is unset or empty, so the header Authorization/);
  });

  it("refuses a template that names no variable, a bad variable name and a bad header name", () => {
    const resolve = withBuildSecrets.resolveUpdatesHeaders;
    expect(() => resolve({ updatesRequestHeaders: { Authorization: "Bearer literal" } }, {}))
      .toThrow(/names no environment variable/);
    expect(() => resolve({ updatesRequestHeaders: { Authorization: "Bearer ${not a name}" } }, {}))
      .toThrow(/names no environment variable/);
    expect(() => resolve({ updatesRequestHeaders: { "Bad Header": "${X}" } }, { X: "v" }))
      .toThrow(/is not a header name/);
    expect(() => resolve({ updatesRequestHeaders: { Authorization: 7 } }, {})).toThrow(/has no template/);
    expect(() => resolve({ updatesRequestHeaders: ["x"] }, {})).toThrow(/maps each header to a template/);
    expect(resolve({}, {})).toEqual({});
  });

  it("fills every variable a template names, and leaves a $ in a value as it is", () => {
    const out = withBuildSecrets.resolveUpdatesHeaders(
      { updatesRequestHeaders: { "X-Pair": "${A}:${B}" } }, { A: "one$&", B: "two" });
    expect(out).toEqual({ "X-Pair": "one$&:two" });
  });

  it("replaces a declared header of the same name in another case, and keeps the others", () => {
    expect(withBuildSecrets.mergeUpdatesHeaders('{"authorization":"Bearer stand-in","X-Channel":"main"}',
                                                { Authorization: "Bearer t" }))
      .toEqual({ "X-Channel": "main", Authorization: "Bearer t" });
    expect(withBuildSecrets.mergeUpdatesHeaders(null, { Authorization: "Bearer t" })).toEqual({ Authorization: "Bearer t" });
    expect(() => withBuildSecrets.mergeUpdatesHeaders("{not json", {})).toThrow(/is not JSON/);
  });
});

describe("buildSecret", () => {
  afterEach(() => setNativeSecrets(undefined));

  it("reads a secret from the native module by name", () => {
    setNativeSecrets({ get: (name) => (name === "apiToken" ? TOKEN : null) });
    expect(buildSecret("apiToken")).toBe(TOKEN);
    expect(buildSecret("other")).toBeUndefined();
  });

  it("is undefined for an empty secret, and where there is no native module", () => {
    setNativeSecrets({ get: () => "" });
    expect(buildSecret("apiToken")).toBeUndefined();
    setNativeSecrets(null);
    expect(buildSecret("apiToken")).toBeUndefined();
  });

  it("finds no native module in Node, and says undefined rather than failing", () => {
    setNativeSecrets(undefined);
    expect(buildSecret("apiToken")).toBeUndefined();
  });
});
