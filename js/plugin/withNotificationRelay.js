/**
 * Expo config plugin that adds a native Android notification listener, which forwards each new
 * notification of chosen apps to an HTTP endpoint as JSON.
 *
 * An assistant that should read a person's messaging apps has no API for most of them, but every
 * message arrives as a notification on the person's phone. A NotificationListenerService sees each
 * one as it is posted, while the phone is locked and asleep and with the app closed. This plugin
 * writes that service in Kotlin and its manifest entry at prebuild time, because `expo prebuild
 * --clean` regenerates android/ and anything edited there by hand is lost.
 *
 * The service forwards only the apps it is given (`packages`), never its own app's notifications
 * and never a group summary. Each notification is sent once (its key and post time). When the
 * endpoint does not answer, the payloads wait in a small file queue, bounded by `queueMax`, and are
 * sent again with a backoff. The service logs counts, never a notification's text.
 *
 * Android asks the person to grant notification access once. Over adb it can be granted with no
 * taps: `adb shell cmd notification allow_listener <app package>/<package>.NotificationRelayService`.
 *
 * Each payload, POSTed with "Content-Type: application/json" and, when a token is set,
 * "Authorization: Bearer <token>":
 *   app              the package that posted it
 *   key              the notification's key
 *   posted_at        when it was posted, in milliseconds since the epoch
 *   conversation     the conversation's title, else the notification's title
 *   conversation_id  the shortcut id, else the tag, else the group key, else the conversation
 *   sender           the notification's title (the sender, in a one-to-one chat)
 *   text             the notification's text
 *   is_group         whether the app marks it a group conversation
 *   messages         for a MessagingStyle notification, each message's sender, text and time (at)
 *
 * Options:
 *   packages               the apps whose notifications are forwarded, by package name (default none)
 *   url                    the endpoint (default: the environment variable named by urlEnv). It must
 *                          be https and not the build machine itself; the prebuild fails otherwise.
 *   urlEnv                 default "EXPO_PUBLIC_NOTIFICATION_RELAY_URL"
 *   token, tokenEnv        the bearer token (default: the environment variable named by tokenEnv,
 *                          "EXPO_PUBLIC_NOTIFICATION_RELAY_TOKEN"; none when unset). It is part of the
 *                          build, so give it a token that may only post to this endpoint.
 *   package                Kotlin package (default: expo.android.package)
 *   buildConfigField, tokenBuildConfigField
 *                          the BuildConfig fields that carry them (default "NOTIFICATION_RELAY_URL",
 *                          "NOTIFICATION_RELAY_TOKEN")
 *   queueMax               the most payloads kept while the endpoint is down (default 200)
 *   logTag                 Android log tag (default "NotificationRelay")
 */
const fs = require("fs");
const path = require("path");
const ntfy = require("./withNtfyForegroundService");

const PACKAGE_NAME = /^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$/;

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
  throw new Error("notification-relay: cannot find expo/config-plugins. Install expo in the app.");
}

/** The endpoint, checked. Throws when it is missing, not https, loopback, or unfit for BuildConfig. */
function checkRelayUrl(url, source) {
  if (!url) {
    throw new Error(`notification-relay: no endpoint. Set ${source} or pass the "url" option.`);
  }
  let parsed = null;
  try {
    parsed = new URL(url);
  } catch (e) {
    parsed = null;
  }
  if (!parsed || parsed.protocol !== "https:") {
    throw new Error(`notification-relay: ${JSON.stringify(url)} is not an https URL. Notifications carry ` +
      "private text, so the endpoint must be https.");
  }
  if (ntfy.isLoopbackHost(parsed.hostname)) {
    throw new Error(`notification-relay: refusing to build with the endpoint ${url}. A phone cannot reach ` +
      "the build machine as localhost.");
  }
  if (/["\\$\s]/.test(url)) {
    throw new Error(`notification-relay: the endpoint contains a character that cannot go into BuildConfig: ${url}`);
  }
  return url;
}

function resolveOptions(config, options = {}, env = process.env) {
  const urlEnv = options.urlEnv || "EXPO_PUBLIC_NOTIFICATION_RELAY_URL";
  const tokenEnv = options.tokenEnv || "EXPO_PUBLIC_NOTIFICATION_RELAY_TOKEN";
  const pkg = options.package || (config.android && config.android.package);
  if (!pkg || !PACKAGE_NAME.test(pkg)) {
    throw new Error(`notification-relay: set expo.android.package or the "package" option (got ${JSON.stringify(pkg)})`);
  }
  const packages = options.packages || [];
  if (!Array.isArray(packages) || packages.some((p) => typeof p !== "string" || !PACKAGE_NAME.test(p))) {
    throw new Error(`notification-relay: "packages" is a list of Android package names (got ${JSON.stringify(packages)})`);
  }
  const queueMax = options.queueMax === undefined ? 200 : options.queueMax;
  if (!Number.isInteger(queueMax) || queueMax < 1 || queueMax > 10000) {
    throw new Error(`notification-relay: "queueMax" is a whole number from 1 to 10000 (got ${JSON.stringify(queueMax)})`);
  }
  const logTag = options.logTag || "NotificationRelay";
  if (!/^[A-Za-z0-9_]{1,23}$/.test(logTag)) {
    throw new Error(`notification-relay: "logTag" is up to 23 letters, digits or underscores (got ${JSON.stringify(logTag)})`);
  }
  return {
    package: pkg,
    packages: [...new Set(packages)],
    url: checkRelayUrl(options.url || env[urlEnv], options.url ? 'the "url" option' : urlEnv),
    token: ntfy.checkToken(options.token || env[tokenEnv], options.token ? 'the "token" option' : tokenEnv),
    buildConfigField: options.buildConfigField || "NOTIFICATION_RELAY_URL",
    tokenBuildConfigField: options.tokenBuildConfigField || "NOTIFICATION_RELAY_TOKEN",
    queueMax,
    logTag,
  };
}

/** The listener service's Kotlin source. */
function renderKotlin(o) {
  const packages = o.packages.map((p) => JSON.stringify(p)).join(", ");
  return `package ${o.package}

import android.app.Notification
import android.app.Person
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** Written by the notification-relay config plugin: forwards new notifications of chosen apps. */
class NotificationRelayService : NotificationListenerService() {
    companion object {
        private const val TAG = "${o.logTag}"
        private val PACKAGES = setOf<String>(${packages})
        private const val QUEUE_MAX = ${o.queueMax}
        private const val QUEUE_FILE = "notification-relay-queue.jsonl"
        private const val BACKOFF_START_MS = 30_000L
        private const val BACKOFF_MAX_MS = 15 * 60_000L
        private val JSON_TYPE = "application/json; charset=utf-8".toMediaType()
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS).readTimeout(15, TimeUnit.SECONDS).build()
    private val worker = Executors.newSingleThreadExecutor()
    private val handler = Handler(Looper.getMainLooper())
    private val sent = object : LinkedHashMap<String, Boolean>(64, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, Boolean>?): Boolean = size > 500
    }
    @Volatile private var backoffMs = BACKOFF_START_MS
    @Volatile private var retryPending = false

    override fun onListenerConnected() {
        worker.execute { flush() }
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        if (sbn.packageName !in PACKAGES || sbn.packageName == packageName) return
        val n = sbn.notification ?: return
        if (n.flags and Notification.FLAG_GROUP_SUMMARY != 0) return
        val once = sbn.key + "@" + sbn.postTime
        synchronized(sent) {
            if (sent.containsKey(once)) return
            sent[once] = true
        }
        val payload = payloadOf(sbn, n) ?: return
        worker.execute { deliver(payload.toString()) }
    }

    private fun text(x: Bundle, name: String): String? = x.getCharSequence(name)?.toString()

    @Suppress("DEPRECATION")
    private fun payloadOf(sbn: StatusBarNotification, n: Notification): JSONObject? {
        val x = n.extras ?: return null
        val title = text(x, Notification.EXTRA_TITLE)
        val body = text(x, Notification.EXTRA_TEXT)
        val conversation = text(x, Notification.EXTRA_CONVERSATION_TITLE) ?: title
        val messages = JSONArray()
        x.getParcelableArray(Notification.EXTRA_MESSAGES)?.forEach { p ->
            val m = p as? Bundle ?: return@forEach
            val said = m.getCharSequence("text")?.toString() ?: return@forEach
            val person = m.getParcelable<Person>("sender_person")
            messages.put(JSONObject()
                .put("sender", person?.name?.toString() ?: m.getCharSequence("sender")?.toString() ?: JSONObject.NULL)
                .put("text", said)
                .put("at", m.getLong("time")))
        }
        if (body == null && messages.length() == 0) return null
        val o = JSONObject()
            .put("app", sbn.packageName)
            .put("key", sbn.key)
            .put("posted_at", sbn.postTime)
            .put("conversation", conversation ?: JSONObject.NULL)
            .put("conversation_id", n.shortcutId ?: sbn.tag ?: n.group ?: conversation ?: JSONObject.NULL)
            .put("sender", title ?: JSONObject.NULL)
            .put("text", body ?: JSONObject.NULL)
            .put("is_group", x.getBoolean(Notification.EXTRA_IS_GROUP_CONVERSATION, false))
        if (messages.length() > 0) o.put("messages", messages)
        return o
    }

    private fun post(json: String): Boolean = try {
        val req = Request.Builder().url(BuildConfig.${o.buildConfigField}).post(json.toRequestBody(JSON_TYPE))
        val token = BuildConfig.${o.tokenBuildConfigField}
        if (token.isNotEmpty()) req.header("Authorization", "Bearer " + token)
        client.newCall(req.build()).execute().use { it.isSuccessful }
    } catch (e: Exception) {
        Log.w(TAG, "post failed: " + e.javaClass.simpleName)
        false
    }

    private fun queueFile(): File = File(filesDir, QUEUE_FILE)

    private fun queued(): List<String> =
        queueFile().takeIf { it.exists() }?.readLines()?.filter { it.isNotBlank() } ?: emptyList()

    private fun keep(lines: List<String>) {
        val f = queueFile()
        if (lines.isEmpty()) { f.delete(); return }
        f.writeText(lines.takeLast(QUEUE_MAX).joinToString("\\n") + "\\n")
    }

    private fun deliver(json: String) {
        if (post(json)) {
            backoffMs = BACKOFF_START_MS
            flush()
        } else {
            keep(queued() + json)
            retryLater()
        }
    }

    private fun flush() {
        val lines = queued()
        if (lines.isEmpty()) return
        val left = ArrayList<String>()
        for ((i, line) in lines.withIndex()) {
            if (!post(line)) { left.addAll(lines.subList(i, lines.size)); break }
        }
        keep(left)
        Log.i(TAG, "sent " + (lines.size - left.size) + " queued, " + left.size + " waiting")
        if (left.isNotEmpty()) retryLater() else backoffMs = BACKOFF_START_MS
    }

    private fun retryLater() {
        if (retryPending) return
        retryPending = true
        val wait = backoffMs
        backoffMs = minOf(backoffMs * 2, BACKOFF_MAX_MS)
        handler.postDelayed({ retryPending = false; worker.execute { flush() } }, wait)
    }
}
`;
}

const LISTENER_PERMISSION = "android.permission.BIND_NOTIFICATION_LISTENER_SERVICE";

/** The manifest (as Expo's parsed XML object) with the listener service and the INTERNET permission. */
function addManifestEntries(manifest, app) {
  manifest["uses-permission"] = manifest["uses-permission"] || [];
  if (!manifest["uses-permission"].some((p) => p.$["android:name"] === "android.permission.INTERNET")) {
    manifest["uses-permission"].push({ $: { "android:name": "android.permission.INTERNET" } });
  }
  // rewritten rather than kept, so a build without --clean cannot carry an older entry forward
  app.service = (app.service || []).filter((s) => s.$["android:name"] !== ".NotificationRelayService");
  app.service.push({
    $: {
      "android:name": ".NotificationRelayService",
      "android:exported": "true",
      "android:permission": LISTENER_PERMISSION,
    },
    "intent-filter": [
      { action: [{ $: { "android:name": "android.service.notification.NotificationListenerService" } }] },
    ],
  });
  return manifest;
}

/** build.gradle with the endpoint and token fields, and the HTTP client the service uses. */
function patchAppGradle(src, o) {
  return ntfy.patchAppGradle(src, {
    buildConfigField: o.buildConfigField, url: o.url,
    tokenBuildConfigField: o.tokenBuildConfigField, token: o.token,
  });
}

function withNotificationRelay(config, options = {}) {
  const { withDangerousMod, withAndroidManifest, AndroidConfig } = loadConfigPlugins(config);

  config = withDangerousMod(config, [
    "android",
    (cfg) => {
      // resolved here and not when the plugin is loaded, so reading the app config never needs it
      const o = resolveOptions(cfg, options);
      const root = cfg.modRequest.platformProjectRoot;
      const dir = path.join(root, "app/src/main/java", ...o.package.split("."));
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, "NotificationRelayService.kt"), renderKotlin(o));
      const gradle = path.join(root, "app/build.gradle");
      fs.writeFileSync(gradle, patchAppGradle(fs.readFileSync(gradle, "utf8"), o));
      return cfg;
    },
  ]);

  config = withAndroidManifest(config, (cfg) => {
    const app = AndroidConfig.Manifest.getMainApplicationOrThrow(cfg.modResults);
    addManifestEntries(cfg.modResults.manifest, app);
    return cfg;
  });
  return config;
}

module.exports = withNotificationRelay;
module.exports.withNotificationRelay = withNotificationRelay;
module.exports.resolveOptions = resolveOptions;
module.exports.checkRelayUrl = checkRelayUrl;
module.exports.renderKotlin = renderKotlin;
module.exports.addManifestEntries = addManifestEntries;
module.exports.patchAppGradle = patchAppGradle;
