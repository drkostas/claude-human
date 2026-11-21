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
