import { cpSync, existsSync, mkdtempSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);
const plugin = require("../app.plugin.js");
const { compileModsAsync } = require("@expo/config-plugins");

const FIXTURE = join(__dirname, "fixture");
const BASE = { name: "Example", slug: "example", scheme: "example", android: { package: "org.example.app" } };

async function prebuild(options: object, env: Record<string, string | undefined> = {}) {
  const root = mkdtempSync(join(tmpdir(), "expo-ntfy-"));
  cpSync(FIXTURE, root, { recursive: true });
  const saved = { ...process.env };
  Object.assign(process.env, env);
  try {
    const config = plugin({ ...BASE, _internal: { projectRoot: root } }, options);
    await compileModsAsync(config, { projectRoot: root, platforms: ["android"], assertMissingModProviders: false });
  } finally {
    process.env = saved;
  }
  const java = join(root, "android/app/src/main/java/org/example/app");
  return {
    root,
    kotlin: readFileSync(join(java, "NtfyService.kt"), "utf8"),
    main: readFileSync(join(java, "MainActivity.kt"), "utf8"),
    gradle: readFileSync(join(root, "android/app/build.gradle"), "utf8"),
    manifest: readFileSync(join(root, "android/app/src/main/AndroidManifest.xml"), "utf8"),
  };
}

describe("config plugin against a fixture project", () => {
  it("writes the service, the receiver, the manifest entries and the URL", async () => {
    const out = await prebuild({}, { EXPO_PUBLIC_NTFY_URL: "https://ntfy.example.org/alerts" });

    expect(out.kotlin).toMatch(/^package org\.example\.app\n/);
    expect(out.kotlin).toContain("class NtfyService : Service()");
    expect(out.kotlin).toContain("class BootReceiver : BroadcastReceiver()");
    expect(out.kotlin).toContain("var topicUrl: String = BuildConfig.NTFY_TOPIC_URL");
    expect(out.kotlin).toContain("FOREGROUND_SERVICE_TYPE_DATA_SYNC");
    expect(out.kotlin).toContain("ACTION_MY_PACKAGE_REPLACED");
    expect(out.kotlin).toContain('click.startsWith("example://")');
    expect(out.kotlin).toContain('.ifBlank { "Example" }');

    expect(out.gradle).toContain('buildConfigField("String", "NTFY_TOPIC_URL", "\\"https://ntfy.example.org/alerts\\"")');
    expect(out.gradle).toContain("buildFeatures { buildConfig true }");
    expect(out.gradle).toContain('implementation("com.squareup.okhttp3:okhttp:4.12.0")');

    expect(out.main).toContain("startForegroundService(svc)");
    expect(out.main.indexOf("super.onCreate(null)")).toBeLessThan(out.main.indexOf("NtfyService::class.java"));

    for (const p of ["FOREGROUND_SERVICE", "FOREGROUND_SERVICE_DATA_SYNC", "RECEIVE_BOOT_COMPLETED", "WAKE_LOCK"]) {
      expect(out.manifest).toContain(`android:name="android.permission.${p}"`);
    }
    expect(out.manifest).toMatch(/<service android:name="\.NtfyService" android:exported="false" android:foregroundServiceType="dataSync"\/>/);
    expect(out.manifest).toContain('<receiver android:name=".BootReceiver" android:exported="true">');
    expect(out.manifest).toContain("android.intent.action.BOOT_COMPLETED");
    expect(out.manifest).toContain("android.intent.action.MY_PACKAGE_REPLACED");
    expect(out.manifest).toContain('android:name="android.permission.INTERNET"');
  });

  it("uses the options for channels, texts, the field name and the link prefix", async () => {
    const out = await prebuild({
      url: "https://push.example.net:8443/lab",
      buildConfigField: "LAB_NTFY_URL",
      ongoingChannelId: "lab_watching",
      alertChannelId: "lab_alerts",
      alertChannelName: "Lab",
      ongoingTitle: 'Lab "is" watching $now',
      defaultTitle: "Lab",
      linkPrefix: "lab://",
    });
    expect(out.kotlin).toContain('const val ONGOING_CHANNEL = "lab_watching"');
    expect(out.kotlin).toContain('const val ALERT_CHANNEL = "lab_alerts"');
    expect(out.kotlin).toContain('.setContentTitle("Lab \\"is\\" watching \\$now")');
    expect(out.kotlin).toContain('click.startsWith("lab://")');
    expect(out.kotlin).toContain("BuildConfig.LAB_NTFY_URL");
    expect(out.gradle).toContain('buildConfigField("String", "LAB_NTFY_URL", "\\"https://push.example.net:8443/lab\\"")');
  });

  it("does not follow any link when the app has no scheme and no prefix is given", async () => {
    const root = mkdtempSync(join(tmpdir(), "expo-ntfy-"));
    cpSync(FIXTURE, root, { recursive: true });
    const config = plugin({ name: "X", slug: "x", android: { package: "org.example.app" }, _internal: { projectRoot: root } },
      { url: "https://ntfy.example.org/t" });
    await compileModsAsync(config, { projectRoot: root, platforms: ["android"], assertMissingModProviders: false });
    const kotlin = readFileSync(join(root, "android/app/src/main/java/org/example/app/NtfyService.kt"), "utf8");
    expect(kotlin).toContain("if (false) {");
  });

  it("refuses a loopback or missing URL at prebuild, but not when the config is only read", async () => {
    // reading the config (expo start, a web build) must not need the phone's address
    expect(() => plugin({ ...BASE }, {})).not.toThrow();
    await expect(prebuild({}, { EXPO_PUBLIC_NTFY_URL: "http://localhost:8080/alerts" })).rejects.toThrow(/localhost/);
    await expect(prebuild({ url: "http://127.0.0.1:8080/a" })).rejects.toThrow(/refusing/);
    await expect(prebuild({}, { EXPO_PUBLIC_NTFY_URL: undefined })).rejects.toThrow(/no ntfy topic URL/);
    await expect(prebuild({ url: "ftp://ntfy.example.org/a" })).rejects.toThrow(/not an http/);
  });
});

describe("plugin helpers", () => {
  const o = { url: "https://n.example.org/a", buildConfigField: "NTFY_TOPIC_URL" };

  it("replaces the URL on a second build instead of keeping the first one", () => {
    const once = plugin.patchAppGradle(readFileSync(join(FIXTURE, "android/app/build.gradle"), "utf8"), o);
    const twice = plugin.patchAppGradle(once, { ...o, url: "https://other.example.org/b" });
    expect(twice).toContain('"\\"https://other.example.org/b\\""');
    expect(twice).not.toContain("n.example.org");
    expect(twice.match(/expo-ntfy-deps/g)).toHaveLength(1);
    expect(twice.match(/buildConfig true/g)).toHaveLength(1);
  });

  it("adds buildConfig to an existing buildFeatures block", () => {
    const src = "android {\n    buildFeatures {\n        viewBinding true\n    }\n    defaultConfig {\n    }\n}\ndependencies {\n}\n";
    const out = plugin.patchAppGradle(src, o);
    expect(out).toContain("buildFeatures {\n        buildConfig true\n        viewBinding true");
  });

  it("starts the service from MainActivity once", () => {
    const src = readFileSync(join(FIXTURE, "android/app/src/main/java/org/example/app/MainActivity.kt"), "utf8");
    const once = plugin.patchMainActivity(src);
    expect(plugin.patchMainActivity(once)).toBe(once);
  });

  it("adds manifest entries once", () => {
    const manifest: { [k: string]: unknown } = {};
    const app: { [k: string]: unknown } = {};
    plugin.addManifestEntries(manifest, app);
    plugin.addManifestEntries(manifest, app);
    expect((manifest["uses-permission"] as unknown[]).length).toBe(4);
    expect((app.service as unknown[]).length).toBe(1);
    expect((app.receiver as unknown[]).length).toBe(1);
  });

  it("validates names that end up in code", () => {
    expect(() => plugin.resolveOptions(BASE, { url: "https://a.example/b", buildConfigField: "bad-name" })).toThrow();
    expect(() => plugin.resolveOptions(BASE, { url: "https://a.example/b", alertChannelId: "a b" })).toThrow();
    expect(() => plugin.resolveOptions({ name: "x" }, { url: "https://a.example/b" })).toThrow(/package/);
    expect(() => plugin.checkTopicUrl('https://a.example/"x', "x")).toThrow();
  });

  it("keeps the fixture itself untouched", () => {
    expect(existsSync(join(FIXTURE, "android/app/src/main/java/org/example/app/NtfyService.kt"))).toBe(false);
  });
});
