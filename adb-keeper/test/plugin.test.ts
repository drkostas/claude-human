import { cpSync, mkdtempSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);
const plugin = require("../app.plugin.js");
const { compileModsAsync } = require("@expo/config-plugins");

// the same small Android project the expo-ntfy plugin is tested against
const FIXTURE = join(__dirname, "../../js/test/fixture");
const BASE = { name: "Example", slug: "example", android: { package: "org.example.app" } };

async function prebuild(options: object = {}, config: object = BASE) {
  const root = mkdtempSync(join(tmpdir(), "expo-adb-keeper-"));
  cpSync(FIXTURE, root, { recursive: true });
  const c = plugin({ ...config, _internal: { projectRoot: root } }, options);
  await compileModsAsync(c, { projectRoot: root, platforms: ["android"], assertMissingModProviders: false });
  return {
    kotlin: readFileSync(join(root, "android/app/src/main/java/org/example/app/AdbKeeper.kt"), "utf8"),
    manifest: readFileSync(join(root, "android/app/src/main/AndroidManifest.xml"), "utf8"),
  };
}

describe("config plugin against a fixture project", () => {
  it("writes the job, the receiver and the manifest entries", async () => {
    const out = await prebuild();
    expect(out.kotlin).toMatch(/^package org\.example\.app\n/);
    expect(out.kotlin).toContain("class AdbKeeperJob : JobService()");
    expect(out.kotlin).toContain("class AdbKeeperReceiver : BroadcastReceiver()");
    expect(out.kotlin).toContain("const val FIXED_PORT = 5555");
    expect(out.kotlin).toContain("ACTION_MY_PACKAGE_REPLACED");
    expect(out.manifest).toContain('android:name="android.permission.WRITE_SECURE_SETTINGS" tools:ignore="ProtectedPermissions"');
    expect(out.manifest).toContain('xmlns:tools="http://schemas.android.com/tools"');
    expect(out.manifest).toContain('android:name=".AdbKeeperJob" android:exported="false" android:permission="android.permission.BIND_JOB_SERVICE"');
    expect(out.manifest).toContain('android:name=".AdbKeeperReceiver" android:exported="true"');
    expect(out.manifest).toContain("android.intent.action.BOOT_COMPLETED");
  });

  it("writes each entry once when it runs twice on the same project", () => {
    const manifest: any = { $: {}, "uses-permission": [] };
    const app: any = { $: {} };
    plugin.addManifestEntries(manifest, app);
    plugin.addManifestEntries(manifest, app);
    const names = manifest["uses-permission"].map((p: any) => p.$["android:name"]);
    expect(names.filter((n: string) => n.endsWith("WRITE_SECURE_SETTINGS"))).toHaveLength(1);
    expect(app.service).toHaveLength(1);
    expect(app.receiver).toHaveLength(1);
  });
});

describe("what the job checks before it turns wireless debugging on", () => {
  const k = plugin.renderKotlin(plugin.resolveOptions(BASE, {}));
  const check = k.slice(k.indexOf("fun check("));
  const put = check.indexOf("Settings.Global.putInt(settings, ADB_WIFI, 1)");

  it("turns it on only after every condition holds, in order", () => {
    const order = [
      "WRITE_SECURE_SETTINGS",
      "Settings.Global.ADB_ENABLED",
      "wireless debugging is on",
      "listening(FIXED_PORT)",
      "TRANSPORT_WIFI",
      "RETRY_MS",
    ].map((s) => check.indexOf(s));
    expect(order.every((i) => i > 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(put).toBeGreaterThan(order[order.length - 1]);
  });

  it("looks for the Wi-Fi network itself, since a VPN is the active network", () => {
    expect(check).toContain("allNetworks.firstOrNull");
    expect(check).not.toContain("activeNetwork");
  });

  it("asks only once per network per retry period, so a prompt does not keep coming back", () => {
    expect(k).toContain("private const val RETRY_MS = 60L * 60_000L");
    expect(check.indexOf('putString("network"')).toBeLessThan(put);
  });

  it("checks only the loopback port, and never touches tcpip itself", () => {
    expect(k).toContain('InetSocketAddress("127.0.0.1", port)');
    expect(k).not.toMatch(/tcpip|setprop|Runtime\.getRuntime/);
  });
});

describe("options", () => {
  it("are applied", () => {
    const o = plugin.resolveOptions(BASE, { fixedPort: 5037, intervalMinutes: 30, retryMinutes: 5, jobId: 7, logTag: "Keeper" });
    const k = plugin.renderKotlin(o);
    expect(k).toContain("const val FIXED_PORT = 5037");
    expect(k).toContain("private const val INTERVAL_MS = 30L * 60_000L");
    expect(k).toContain("private const val SOON_ID = 7");
    expect(k).toContain("private const val PERIODIC_ID = 8");
    expect(k).toContain('const val TAG = "Keeper"');
  });

  it("refuse values that cannot work", () => {
    expect(() => plugin.resolveOptions(BASE, { intervalMinutes: 5 })).toThrow(/intervalMinutes/);
    expect(() => plugin.resolveOptions(BASE, { fixedPort: 70000 })).toThrow(/fixedPort/);
    expect(() => plugin.resolveOptions({ name: "x" }, {})).toThrow(/android.package/);
  });
});
