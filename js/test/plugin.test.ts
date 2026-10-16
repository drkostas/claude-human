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
    expect(out.kotlin).toContain("FOREGROUND_SERVICE_TYPE_SPECIAL_USE");
    expect(out.kotlin).not.toContain("FOREGROUND_SERVICE_TYPE_DATA_SYNC");
    expect(out.kotlin).toContain("ACTION_MY_PACKAGE_REPLACED");
    expect(out.kotlin).toContain('private fun intoApp(link: String): Boolean = link.startsWith("example://")');
    expect(out.kotlin).toContain('.ifBlank { "Example" }');
    // A tap on a notification opens its link with a link intent aimed at the app. The launcher
    // intent with a link attached only brought a running app to the front, without the link.
    // A button opens its link the same way.
    const link = out.kotlin.slice(out.kotlin.indexOf("private fun linkIntent("));
    expect(link).toContain("Intent(Intent.ACTION_VIEW, android.net.Uri.parse(link))");
    expect(link).toContain("setPackage(packageName)");
    expect(link).toContain("FLAG_ACTIVITY_SINGLE_TOP");
    const handle = out.kotlin.slice(out.kotlin.indexOf("private fun handle("));
    expect(handle).toContain(
      "val open = if (intoApp(click)) linkIntent(click) else packageManager.getLaunchIntentForPackage(packageName)");

    expect(out.gradle).toContain('buildConfigField("String", "NTFY_TOPIC_URL", "\\"https://ntfy.example.org/alerts\\"")');
    expect(out.gradle).toContain("buildFeatures { buildConfig true }");
    expect(out.gradle).toContain('implementation("com.squareup.okhttp3:okhttp:4.12.0")');

    expect(out.main).toContain("startForegroundService(svc)");
    expect(out.main.indexOf("super.onCreate(null)")).toBeLessThan(out.main.indexOf("NtfyService::class.java"));

    for (const p of ["FOREGROUND_SERVICE", "FOREGROUND_SERVICE_SPECIAL_USE", "RECEIVE_BOOT_COMPLETED", "WAKE_LOCK"]) {
      expect(out.manifest).toContain(`android:name="android.permission.${p}"`);
    }
    // Android 15 stops a dataSync service after 6 hours in 24 and refuses to restart it.
    expect(out.manifest).toMatch(/<service android:name="\.NtfyService" android:exported="false" android:foregroundServiceType="specialUse">/);
    expect(out.manifest).toContain('android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"');
    expect(out.manifest).not.toContain("dataSync");
    expect(out.manifest).not.toContain("FOREGROUND_SERVICE_DATA_SYNC");
    expect(out.manifest).toContain('<receiver android:name=".BootReceiver" android:exported="true">');
    expect(out.manifest).toContain("android.intent.action.BOOT_COMPLETED");
    expect(out.manifest).toContain("android.intent.action.MY_PACKAGE_REPLACED");
    // The listener can be started from outside (adb) when an update did not restart it.
    expect(out.manifest).toContain('android:name="${applicationId}.START_LISTENER"');
    expect(out.kotlin).toContain('intent.action == context.packageName + ".START_LISTENER"');
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
    expect(out.kotlin).toContain('link.startsWith("lab://")');
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
    expect(kotlin).toContain("private fun intoApp(link: String): Boolean = false");
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
    expect((manifest["uses-permission"] as unknown[]).length).toBe(5);
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

describe("the service type on a manifest that already has the service", () => {
  it("replaces an old dataSync entry and its permission, so a build without --clean is fixed too", () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const manifest: Record<string, any[]> = {
      "uses-permission": [{ $: { "android:name": "android.permission.FOREGROUND_SERVICE_DATA_SYNC" } }],
    };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const app: { service: any[] } = {
      service: [{ $: { "android:name": ".NtfyService", "android:exported": "false",
                       "android:foregroundServiceType": "dataSync" } }],
    };
    plugin.addManifestEntries(manifest, app);
    const ours = app.service.filter((s) => s.$["android:name"] === ".NtfyService");
    expect(ours).toHaveLength(1);
    expect(ours[0].$["android:foregroundServiceType"]).toBe("specialUse");
    expect(ours[0].property[0].$["android:name"]).toBe("android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE");
    const perms = manifest["uses-permission"].map((p) => p.$["android:name"]);
    expect(perms).not.toContain("android.permission.FOREGROUND_SERVICE_DATA_SYNC");
    expect(perms).toContain("android.permission.FOREGROUND_SERVICE_SPECIAL_USE");
  });
});

describe("what a message shows", () => {
  const ENV = { EXPO_PUBLIC_NTFY_URL: "https://ntfy.example.org/alerts" };
  /** One function of the generated service, from its name to the next function. */
  const fn = (kotlin: string, name: string) => {
    const start = kotlin.indexOf(`private fun ${name}(`);
    expect(start, `no function ${name}`).toBeGreaterThan(-1);
    const next = kotlin.indexOf("\n    private fun ", start + 1);
    return kotlin.slice(start, next === -1 ? undefined : next);
  };

  it("draws each view action into the app as a button, at most three, each with its own request code", async () => {
    const { kotlin } = await prebuild({}, ENV);
    expect(fn(kotlin, "handle")).toContain('for ((label, tap) in buttons(id, f.optJSONArray("actions"))) addAction(0, label, tap)');
    const buttons = fn(kotlin, "buttons");
    expect(buttons).toContain("if (out.size == MAX_BUTTONS) break");
    expect(buttons).toContain('a.optString("action") != "view"');
    expect(buttons).toContain("!intoApp(url)");
    expect(buttons).toContain('PendingIntent.getActivity(this, "$id#$i".hashCode(), linkIntent(url), FLAGS)');
    expect(kotlin).toContain("const val MAX_BUTTONS = 3");
    expect(kotlin).toContain("const val FLAGS = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT");
  });

  it("posts on the alarm, chime or silent channel by the message's priority", async () => {
    const { kotlin, manifest } = await prebuild({}, ENV);
    const handle = fn(kotlin, "handle");
    expect(handle).toContain('val channel = channelFor(f.optInt("priority", 3))');
    expect(handle).toContain("NotificationCompat.Builder(this, channel)");
    const pick = fn(kotlin, "channelFor");
    expect(pick).toContain("priority >= ALARM_FROM -> ALARM_CHANNEL");
    expect(pick).toContain("priority >= CHIME_FROM -> ALERT_CHANNEL");
    expect(pick).toContain("else -> SILENT_CHANNEL");
    expect(kotlin).toContain("const val ALARM_FROM = 5");
    expect(kotlin).toContain("const val CHIME_FROM = 4");
    const channels = fn(kotlin, "createChannels");
    expect(channels).toContain('NotificationChannel(ALERT_CHANNEL, "Example", NotificationManager.IMPORTANCE_HIGH)');
    expect(channels).toContain('NotificationChannel(ALARM_CHANNEL, "Urgent", NotificationManager.IMPORTANCE_HIGH)');
    expect(channels).toContain("setUsage(AudioAttributes.USAGE_ALARM)");
    expect(channels).toContain("setBypassDnd(true)");
    expect(channels).toContain('NotificationChannel(SILENT_CHANNEL, "Quiet", NotificationManager.IMPORTANCE_LOW)');
    // Do Not Disturb access is what lets the alarm channel keep its bypass, and Android lists an
    // app for that access only when the manifest asks for it.
    expect(manifest).toContain('android:name="android.permission.ACCESS_NOTIFICATION_POLICY"');
  });

  it("folds to the title and the message's first line, and pulls down to the whole message", async () => {
    const { kotlin } = await prebuild({}, ENV);
    const handle = fn(kotlin, "handle");
    expect(handle).toContain(".setContentText(firstLine(body))");
    expect(handle).toContain(".setStyle(NotificationCompat.BigTextStyle().bigText(body))");
    expect(handle).not.toContain(".setContentText(body)");
    expect(fn(kotlin, "firstLine")).toContain("body.lineSequence().map { it.trim() }.firstOrNull { it.isNotEmpty() } ?: body");
  });

  it("shows a tag that has the sub text prefix in the header, and no tag without the option", async () => {
    const plain = await prebuild({}, ENV);
    expect(plain.kotlin).toContain('private const val SUBTEXT_TAG_PREFIX = ""');
    const tagged = await prebuild({ subTextTagPrefix: "area:" }, ENV);
    expect(tagged.kotlin).toContain('private const val SUBTEXT_TAG_PREFIX = "area:"');
    expect(fn(tagged.kotlin, "handle")).toContain('subText(f.optJSONArray("tags"))?.let { setSubText(it) }');
  });

  it("names a notification by its key tag, so a later one replaces it and a clear tag takes it back", async () => {
    const plain = await prebuild({}, ENV);
    expect(plain.kotlin).toContain('private const val KEY_TAG_PREFIX = ""');
    expect(plain.kotlin).toContain('private const val CLEAR_TAG = ""');
    const keyed = await prebuild({ keyTagPrefix: "key:", clearTag: "clear" }, ENV);
    expect(keyed.kotlin).toContain('private const val KEY_TAG_PREFIX = "key:"');
    expect(keyed.kotlin).toContain('private const val CLEAR_TAG = "clear"');
    const handle = fn(keyed.kotlin, "handle");
    expect(handle).toContain('val shown = (key ?: id).hashCode()');
    expect(handle).toContain(".cancel(shown)");
    expect(handle).toContain(".notify(shown, n)");
    // the clear is checked before anything is drawn, so a clear message never shows
    expect(handle.indexOf(".cancel(shown)")).toBeLessThan(handle.indexOf("NotificationCompat.Builder"));
  });

  it("uses the options for the new channels and the priorities, and refuses bad ones", async () => {
    const { kotlin } = await prebuild({
      alarmChannelId: "lab_alarm", alarmChannelName: "Lab alarms",
      silentChannelId: "lab_quiet", silentChannelName: "Lab results",
      alarmPriority: 4, chimePriority: 3,
    }, ENV);
    expect(kotlin).toContain('const val ALARM_CHANNEL = "lab_alarm"');
    expect(kotlin).toContain('const val SILENT_CHANNEL = "lab_quiet"');
    expect(kotlin).toContain('NotificationChannel(ALARM_CHANNEL, "Lab alarms", NotificationManager.IMPORTANCE_HIGH)');
    expect(kotlin).toContain("const val ALARM_FROM = 4");
    expect(kotlin).toContain("const val CHIME_FROM = 3");
    const url = "https://a.example/b";
    expect(() => plugin.resolveOptions(BASE, { url, alarmPriority: 6 })).toThrow(/alarmPriority/);
    expect(() => plugin.resolveOptions(BASE, { url, chimePriority: 2.5 })).toThrow(/chimePriority/);
    expect(() => plugin.resolveOptions(BASE, { url, alarmPriority: 4, chimePriority: 5 })).toThrow(/above/);
    expect(() => plugin.resolveOptions(BASE, { url, silentChannelId: "ntfy_messages" })).toThrow(/differ/);
    expect(() => plugin.resolveOptions(BASE, { url, alarmChannelId: "a b" })).toThrow(/alarmChannelId/);
  });
});

describe("an access token for a server that requires authentication", () => {
  // built at run time, so no scanner reads a literal as a real token
  const token = ["tk", "example", "readonly", "0123"].join("_");

  it("is written to BuildConfig and sent as a Bearer header when the service connects", async () => {
    const { gradle, kotlin } = await prebuild({ url: "https://n.example.org/a" }, { EXPO_PUBLIC_NTFY_TOKEN: token });
    expect(gradle).toContain(`buildConfigField("String", "NTFY_TOKEN", "\\"${token}\\"")`);
    expect(kotlin).toContain("val token = BuildConfig.NTFY_TOKEN");
    expect(kotlin).toContain('req.header("Authorization", "Bearer $token")');
    expect(kotlin).not.toContain(token);
  });

  it("is an empty field without one, so the service connects without a header", async () => {
    const { gradle } = await prebuild({ url: "https://n.example.org/a" }, { EXPO_PUBLIC_NTFY_TOKEN: undefined });
    expect(gradle).toContain('buildConfigField("String", "NTFY_TOKEN", "\\"\\"")');
  });

  it("refuses a token BuildConfig cannot hold, and a field name the URL already uses", () => {
    const url = "https://n.example.org/a";
    expect(() => plugin.resolveOptions(BASE, { url, token: 'a"b' })).toThrow(/BuildConfig/);
    expect(() => plugin.resolveOptions(BASE, { url, tokenBuildConfigField: "NTFY_TOPIC_URL" })).toThrow(/differ/);
    expect(plugin.resolveOptions(BASE, { url, token }).token).toBe(token);
  });
});

