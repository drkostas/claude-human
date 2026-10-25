import { cpSync, mkdtempSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);
const relay = require("../plugin/withNotificationRelay.js");
const { compileModsAsync } = require("@expo/config-plugins");

const FIXTURE = join(__dirname, "fixture");
const BASE = { name: "Example", slug: "example", scheme: "example", android: { package: "org.example.app" } };
const URL_ENV = { EXPO_PUBLIC_NOTIFICATION_RELAY_URL: "https://relay.example.org/messages" };

async function prebuild(options: object, env: Record<string, string | undefined> = URL_ENV) {
  const root = mkdtempSync(join(tmpdir(), "notification-relay-"));
  cpSync(FIXTURE, root, { recursive: true });
  const saved = { ...process.env };
  Object.assign(process.env, env);
  try {
    const config = relay({ ...BASE, _internal: { projectRoot: root } }, options);
    await compileModsAsync(config, { projectRoot: root, platforms: ["android"], assertMissingModProviders: false });
  } finally {
    process.env = saved;
  }
  const java = join(root, "android/app/src/main/java/org/example/app");
  return {
    kotlin: readFileSync(join(java, "NotificationRelayService.kt"), "utf8"),
    gradle: readFileSync(join(root, "android/app/build.gradle"), "utf8"),
    manifest: readFileSync(join(root, "android/app/src/main/AndroidManifest.xml"), "utf8"),
  };
}

describe("notification relay plugin against a fixture project", () => {
  it("writes the listener, its manifest entry and the endpoint", async () => {
    const out = await prebuild({ packages: ["com.example.chat", "com.example.messenger"] },
      { ...URL_ENV, EXPO_PUBLIC_NOTIFICATION_RELAY_TOKEN: "tok-123" });
    expect(out.kotlin).toMatch(/^package org\.example\.app\n/);
    expect(out.kotlin).toContain("class NotificationRelayService : NotificationListenerService()");
    expect(out.kotlin).toContain('setOf<String>("com.example.chat", "com.example.messenger")');
    expect(out.manifest).toContain('android:name=".NotificationRelayService"');
    expect(out.manifest).toContain('android:permission="android.permission.BIND_NOTIFICATION_LISTENER_SERVICE"');
    expect(out.manifest).toContain("android.service.notification.NotificationListenerService");
    expect(out.manifest).toContain("android.permission.INTERNET");
    expect(out.gradle).toContain('buildConfigField("String", "NOTIFICATION_RELAY_URL", "\\"https://relay.example.org/messages\\"")');
    expect(out.gradle).toContain('buildConfigField("String", "NOTIFICATION_RELAY_TOKEN", "\\"tok-123\\"")');
    expect(out.gradle).toContain("okhttp");
  });

  it("is written again, not twice, by a second prebuild", async () => {
    const root = mkdtempSync(join(tmpdir(), "notification-relay-"));
    cpSync(FIXTURE, root, { recursive: true });
    const saved = { ...process.env };
    Object.assign(process.env, URL_ENV);
    try {
      for (let i = 0; i < 2; i++) {
        const config = relay({ ...BASE, _internal: { projectRoot: root } }, { packages: ["com.example.chat"] });
        await compileModsAsync(config, { projectRoot: root, platforms: ["android"], assertMissingModProviders: false });
      }
    } finally {
      process.env = saved;
    }
    const manifest = readFileSync(join(root, "android/app/src/main/AndroidManifest.xml"), "utf8");
    expect(manifest.split('android:name=".NotificationRelayService"').length - 1).toBe(1);
  });
});

describe("what the listener forwards", () => {
  const o = relay.resolveOptions(BASE, { packages: ["com.example.chat"], url: "https://relay.example.org/m" }, {});
  const kotlin: string = relay.renderKotlin(o);

  it("forwards only the listed apps, never its own, and never a group summary", () => {
    expect(kotlin).toContain("sbn.packageName !in PACKAGES || sbn.packageName == packageName");
    expect(kotlin).toContain("Notification.FLAG_GROUP_SUMMARY != 0) return");
  });

  it("sends each notification once, by its key and post time", () => {
    expect(kotlin).toContain('val once = sbn.key + "@" + sbn.postTime');
  });

  it("carries the conversation, the sender, the text and a MessagingStyle's messages", () => {
    for (const field of ['"app"', '"key"', '"posted_at"', '"conversation"', '"conversation_id"', '"sender"',
                         '"text"', '"is_group"', '"messages"']) {
      expect(kotlin).toContain(`.put(${field}`);
    }
    expect(kotlin).toContain("Notification.EXTRA_MESSAGES");
  });

  it("queues what the endpoint did not take, bounded, and never logs a notification's text", () => {
    expect(kotlin).toContain("takeLast(QUEUE_MAX)");
    expect(kotlin).toContain("private const val QUEUE_MAX = 200");
    const logs = kotlin.split("\n").filter((l) => /Log\.[iwed]\(/.test(l));
    expect(logs.length).toBeGreaterThan(0);
    for (const line of logs) expect(line).not.toMatch(/body|said|json|payload/);
  });

  it("sends the token only when there is one", () => {
    expect(kotlin).toContain('if (token.isNotEmpty()) req.header("Authorization", "Bearer " + token)');
  });
});

describe("the options", () => {
  it("refuses an endpoint that is missing, not https, or the build machine itself", () => {
    expect(() => relay.resolveOptions(BASE, {}, {})).toThrow(/no endpoint/);
    expect(() => relay.resolveOptions(BASE, { url: "http://relay.example.org/m" }, {})).toThrow(/not an https URL/);
    expect(() => relay.resolveOptions(BASE, { url: "https://localhost:8787/m" }, {})).toThrow(/localhost/);
    expect(() => relay.resolveOptions(BASE, { url: "https://127.0.0.1/m" }, {})).toThrow(/localhost/);
  });

  it("refuses a package list that is not package names, and a queue size out of range", () => {
    expect(() => relay.resolveOptions(BASE, { url: "https://relay.example.org/m", packages: ["not a package"] }, {}))
      .toThrow(/package names/);
    expect(() => relay.resolveOptions(BASE, { url: "https://relay.example.org/m", queueMax: 0 }, {}))
      .toThrow(/queueMax/);
  });

  it("reads the endpoint and token from their environment variables", () => {
    const o = relay.resolveOptions(BASE, {}, { EXPO_PUBLIC_NOTIFICATION_RELAY_URL: "https://relay.example.org/m",
                                              EXPO_PUBLIC_NOTIFICATION_RELAY_TOKEN: "abc" });
    expect(o.url).toBe("https://relay.example.org/m");
    expect(o.token).toBe("abc");
    expect(o.packages).toEqual([]);
  });
});
