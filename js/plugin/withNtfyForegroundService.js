/**
 * Expo config plugin that adds a native Android foreground service holding an ntfy WebSocket.
 *
 * Without Firebase, a socket opened from JavaScript lives only while the app is in the foreground:
 * Android suspends it seconds after the screen locks. A foreground service can keep it open, which
 * is what ntfy's own Android app does for self-hosted servers. This plugin writes a small Kotlin
 * service that owns the socket and posts each message to the tray itself, a receiver that starts
 * it again after a reboot or an app update, and the manifest entries they need.
 *
 * The source and the manifest changes are written at prebuild time, because `expo prebuild
 * --clean` regenerates android/ and anything edited there by hand is lost.
 *
 * Options (all optional):
 *   url               the topic URL (default: the environment variable named by urlEnv)
 *   urlEnv            default "EXPO_PUBLIC_NTFY_URL"
 *   package           Kotlin package and manifest namespace (default: expo.android.package)
 *   buildConfigField  name of the BuildConfig field that carries the URL (default "NTFY_TOPIC_URL")
 *   defaultTitle      title for a message without one (default: expo.name)
 *   linkPrefix        a message's click URL opens the app only when it starts with this
 *                     (default: "<expo.scheme>://", or no links when the app has no scheme)
 *   ongoingChannelId, ongoingChannelName, ongoingChannelDescription, ongoingTitle, ongoingText
 *                     the low-importance channel and notification a foreground service must show
 *   alertChannelId, alertChannelName, alertChannelDescription
 *                     the chime channel, for messages from chimePriority up to alarmPriority. It
 *                     keeps the id every message was posted on before there were three channels,
 *                     so the settings a person chose for it carry over.
 *   alarmChannelId, alarmChannelName, alarmChannelDescription
 *                     the alarm channel, for messages at alarmPriority or above: the phone's alarm
 *                     sound, played as an alarm, and through Do Not Disturb where Android allows it
 *   silentChannelId, silentChannelName, silentChannelDescription
 *                     the silent channel, for messages below chimePriority: in the tray, no sound
 *   alarmPriority     the lowest ntfy priority that rings the alarm (default 5)
 *   chimePriority     the lowest ntfy priority that chimes (default 4). ntfy's default priority is
 *                     3, so a message published without one is silent unless this is set to 3.
 *   subTextTagPrefix  a tag that starts with this is shown, without it, next to the app's name in
 *                     the notification's header (default "": no tag is shown)
 *   logTag            Android log tag (default "NtfyService")
 *
 * The Android prebuild fails when the URL is missing or points at the build machine itself (localhost,
 * 127.0.0.1, ::1): a phone given that address connects to itself and never receives anything.
 */
const fs = require("fs");
const path = require("path");

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
  throw new Error("expo-ntfy: cannot find expo/config-plugins. Install expo in the app.");
}

function isLoopbackHost(host) {
  const h = String(host || "").replace(/^\[|\]$/g, "").toLowerCase();
  return h === "" || h === "localhost" || h.endsWith(".localhost") || h === "::1" || h === "0.0.0.0"
    || /^127\.\d+\.\d+\.\d+$/.test(h);
}

/** The topic URL, checked. Throws when it is missing, invalid or loopback. */
function checkTopicUrl(url, source) {
  let parsed = null;
  try {
    parsed = new URL(url);
  } catch (e) {
    parsed = null;
  }
  if (!url) {
    throw new Error(`expo-ntfy: no ntfy topic URL. Set ${source} or pass the "url" option.`);
  }
  if (!parsed || !/^https?:$/.test(parsed.protocol)) {
    throw new Error(`expo-ntfy: ${JSON.stringify(url)} is not an http(s) URL.`);
  }
  if (isLoopbackHost(parsed.hostname)) {
    throw new Error(`expo-ntfy: refusing to build with ntfy at ${url}. A phone cannot reach the ` +
      "build machine as localhost. Use an address of the ntfy server that the phone can reach.");
  }
  if (/["\\$\s]/.test(url)) {
    throw new Error(`expo-ntfy: the topic URL contains a character that cannot go into BuildConfig: ${url}`);
  }
  return url;
}

function resolveOptions(config, options = {}, env = process.env) {
  const name = config.name || "App";
  const urlEnv = options.urlEnv || "EXPO_PUBLIC_NTFY_URL";
  const pkg = options.package || (config.android && config.android.package);
  if (!pkg || !/^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$/.test(pkg)) {
    throw new Error(`expo-ntfy: set expo.android.package or the "package" option (got ${JSON.stringify(pkg)})`);
  }
  const scheme = Array.isArray(config.scheme) ? config.scheme[0] : config.scheme;
  const o = {
    url: checkTopicUrl(options.url || env[urlEnv], options.url ? 'the "url" option' : urlEnv),
    package: pkg,
    buildConfigField: options.buildConfigField || "NTFY_TOPIC_URL",
    defaultTitle: options.defaultTitle || name,
    linkPrefix: options.linkPrefix !== undefined ? options.linkPrefix : (scheme ? `${scheme}://` : ""),
    ongoingChannelId: options.ongoingChannelId || "ntfy_connection",
    ongoingChannelName: options.ongoingChannelName || "Connection",
    ongoingChannelDescription: options.ongoingChannelDescription ||
      "Keeps the app connected so messages arrive while it is closed",
    ongoingTitle: options.ongoingTitle || `${name} is connected`,
    ongoingText: options.ongoingText || "waiting for messages",
    alertChannelId: options.alertChannelId || "ntfy_messages",
    alertChannelName: options.alertChannelName || name,
    alertChannelDescription: options.alertChannelDescription || "Messages from the server",
    alarmChannelId: options.alarmChannelId || "ntfy_alarm",
    alarmChannelName: options.alarmChannelName || "Urgent",
    alarmChannelDescription: options.alarmChannelDescription ||
      "Messages at the highest priority, with the alarm sound",
    silentChannelId: options.silentChannelId || "ntfy_silent",
    silentChannelName: options.silentChannelName || "Quiet",
    silentChannelDescription: options.silentChannelDescription || "Messages at a low priority, without a sound",
    alarmPriority: options.alarmPriority !== undefined ? options.alarmPriority : 5,
    chimePriority: options.chimePriority !== undefined ? options.chimePriority : 4,
    subTextTagPrefix: options.subTextTagPrefix || "",
    logTag: options.logTag || "NtfyService",
  };
  if (!/^[A-Z_][A-Z0-9_]*$/.test(o.buildConfigField)) {
    throw new Error(`expo-ntfy: buildConfigField must be an upper case identifier (got ${o.buildConfigField})`);
  }
  const channels = ["ongoingChannelId", "alertChannelId", "alarmChannelId", "silentChannelId"];
  for (const k of channels) {
    if (!/^[A-Za-z0-9_.-]+$/.test(o[k])) throw new Error(`expo-ntfy: invalid ${k} ${JSON.stringify(o[k])}`);
  }
  if (new Set(channels.map((k) => o[k])).size !== channels.length) {
    throw new Error(`expo-ntfy: the four channel ids must differ (got ${channels.map((k) => o[k]).join(", ")})`);
  }
  // ntfy priorities run from 1 (min) to 5 (max), and they are written into the service as numbers
  for (const k of ["alarmPriority", "chimePriority"]) {
    if (!Number.isInteger(o[k]) || o[k] < 1 || o[k] > 5) {
      throw new Error(`expo-ntfy: ${k} must be a whole ntfy priority from 1 to 5 (got ${JSON.stringify(o[k])})`);
    }
  }
  if (o.chimePriority > o.alarmPriority) {
    throw new Error(`expo-ntfy: chimePriority (${o.chimePriority}) is above alarmPriority (${o.alarmPriority})`);
  }
  return o;
}

/** A Kotlin string literal. */
function kt(s) {
  return '"' + String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\$/g, "\\$")
    .replace(/\n/g, "\\n") + '"';
}

function renderKotlin(o) {
  const linkCheck = o.linkPrefix
    ? `link.startsWith(${kt(o.linkPrefix)})`
    : "false";
  return `package ${o.package}

import android.app.*
import android.content.*
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import okhttp3.*
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Holds the ntfy topic's WebSocket open natively, so messages arrive while the phone is locked and
 * the JavaScript side is suspended. It posts what it receives to the tray itself and does not talk
 * to JavaScript. Written by the expo-ntfy config plugin; regenerated on every prebuild.
 *
 * A message is posted on one of three channels by its ntfy priority (an alarm, a chime, or no
 * sound). It folds to its title and first line, pulls down to the whole message, and carries a
 * button for each of its view actions that links into the app.
 */
class NtfyService : Service() {
    companion object {
        const val ONGOING_CHANNEL = ${kt(o.ongoingChannelId)}
        /** the chime channel, which keeps the id messages were always posted on */
        const val ALERT_CHANNEL = ${kt(o.alertChannelId)}
        const val ALARM_CHANNEL = ${kt(o.alarmChannelId)}
        const val SILENT_CHANNEL = ${kt(o.silentChannelId)}
        const val ONGOING_ID = 4711
        /** the lowest ntfy priorities that ring the alarm and that chime; below CHIME_FROM is silent */
        const val ALARM_FROM = ${o.alarmPriority}
        const val CHIME_FROM = ${o.chimePriority}
        /** Android's notification templates show at most three buttons */
        const val MAX_BUTTONS = 3
        /** Android 12 and later require every PendingIntent to say whether it is mutable, and
         *  Android 14 and later refuse a mutable one whose intent names no app. These are immutable,
         *  and each intent names this app's package. */
        const val FLAGS = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        private const val SUBTEXT_TAG_PREFIX = ${kt(o.subTextTagPrefix)}
        private const val TAG = ${kt(o.logTag)}
        var topicUrl: String = BuildConfig.${o.buildConfigField}
    }

    private var client: OkHttpClient? = null
    private var ws: WebSocket? = null
    private var stopping = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannels()
        // Android 14 and later throw unless the service type is also given here, not only in the
        // manifest. specialUse, never dataSync: Android 15 gives dataSync 6 hours in any 24, then
        // stops the service and refuses to start it again until the app is opened.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(
                ONGOING_ID, ongoingNotification(),
                android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
            )
        } else {
            startForeground(ONGOING_ID, ongoingNotification())
        }
        connect()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        stopping = true
        ws?.close(1000, "service stopping")
        super.onDestroy()
    }

    private fun createChannels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val mgr = getSystemService(NotificationManager::class.java)
        // The connection notice is required for a foreground service. It is not a message, so its
        // channel has the lowest importance.
        mgr.createNotificationChannel(
            NotificationChannel(ONGOING_CHANNEL, ${kt(o.ongoingChannelName)}, NotificationManager.IMPORTANCE_MIN)
                .apply { description = ${kt(o.ongoingChannelDescription)} }
        )
        mgr.createNotificationChannel(
            NotificationChannel(ALERT_CHANNEL, ${kt(o.alertChannelName)}, NotificationManager.IMPORTANCE_HIGH)
                .apply { description = ${kt(o.alertChannelDescription)} }
        )
        // The alarm plays the phone's alarm sound as an alarm, so it rings at the alarm volume and
        // Do Not Disturb lets it through wherever it lets alarms through. Android keeps the
        // channel's own bypass only when the app has Do Not Disturb access (the person grants it in
        // the system settings) and clears it otherwise. The channels are created at every start,
        // so access granted later applies at the next start, unless the person changed the channel.
        // Android fixes a channel's sound when it is first created, so the alarm has its own id.
        mgr.createNotificationChannel(
            NotificationChannel(ALARM_CHANNEL, ${kt(o.alarmChannelName)}, NotificationManager.IMPORTANCE_HIGH)
                .apply {
                    description = ${kt(o.alarmChannelDescription)}
                    setSound(
                        RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM),
                        AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_ALARM)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                            .build()
                    )
                    enableVibration(true)
                    setBypassDnd(true)
                }
        )
        // Silent: in the tray and on the lock screen, with no sound and no vibration.
        mgr.createNotificationChannel(
            NotificationChannel(SILENT_CHANNEL, ${kt(o.silentChannelName)}, NotificationManager.IMPORTANCE_LOW)
                .apply { description = ${kt(o.silentChannelDescription)} }
        )
    }

    private fun ongoingNotification(): Notification =
        NotificationCompat.Builder(this, ONGOING_CHANNEL)
            .setContentTitle(${kt(o.ongoingTitle)})
            .setContentText(${kt(o.ongoingText)})
            .setSmallIcon(applicationInfo.icon)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setContentIntent(
                PendingIntent.getActivity(
                    this, 0,
                    packageManager.getLaunchIntentForPackage(packageName),
                    PendingIntent.FLAG_IMMUTABLE
                )
            )
            .build()

    private fun wsUrl(): String {
        var base = topicUrl.trimEnd('/')
        base = base.replaceFirst("https://", "wss://").replaceFirst("http://", "ws://")
        return base + "/ws"
    }

    private fun connect() {
        if (stopping) return
        val c = OkHttpClient.Builder()
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .pingInterval(45, TimeUnit.SECONDS)   // keeps NAT mappings and tunnels open
            .build()
        client = c
        ws = c.newWebSocket(Request.Builder().url(wsUrl()).build(), object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) = handle(text)
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "socket failed: " + t.message)
                reconnectSoon()
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = reconnectSoon()
        })
    }

    private fun reconnectSoon() {
        if (stopping) return
        Thread {
            Thread.sleep(5000)
            if (!stopping) connect()
        }.start()
    }

    /** Only message frames are shown. Open and keepalive frames are not for a person. */
    private fun handle(text: String) {
        try {
            val f = JSONObject(text)
            if (f.optString("event") != "message") return
            val id = f.optString("id")
            val title = f.optString("title").ifBlank { ${kt(o.defaultTitle)} }
            val body = f.optString("message").ifBlank { title }
            val click = f.optString("click")
            val open = if (intoApp(click)) linkIntent(click) else packageManager.getLaunchIntentForPackage(packageName)
            // ntfy leaves the priority out for its default, 3
            val channel = channelFor(f.optInt("priority", 3))
            val n = NotificationCompat.Builder(this, channel)
                .setContentTitle(title)
                // folded, on the lock screen and in the shade: the title and the first line
                .setContentText(firstLine(body))
                // pulled down: the whole message, and the buttons
                .setStyle(NotificationCompat.BigTextStyle().bigText(body))
                .setSmallIcon(applicationInfo.icon)
                .setAutoCancel(true)
                .setPriority(priorityFor(channel))
                .apply {
                    subText(f.optJSONArray("tags"))?.let { setSubText(it) }
                    if (open != null) setContentIntent(
                        PendingIntent.getActivity(this@NtfyService, click.hashCode(), open, FLAGS)
                    )
                    for ((label, tap) in buttons(id, f.optJSONArray("actions"))) addAction(0, label, tap)
                }
                .build()
            getSystemService(NotificationManager::class.java).notify(id.hashCode(), n)
        } catch (e: Exception) {
            Log.w(TAG, "bad frame: " + e.message)
        }
    }

    /** Only links into this app are followed. A link in a notification is data from the network. */
    private fun intoApp(link: String): Boolean = ${linkCheck}

    /**
     * A link opens as a link intent aimed at this app, not as the launcher intent with a link
     * attached: the launcher intent carries CATEGORY_LAUNCHER, and when the app is already running
     * Android then only brings its task to the front and never delivers the link, so a tap opened
     * the app on whatever screen it was last on. The app's own link handling takes it from here.
     */
    private fun linkIntent(link: String): Intent =
        Intent(Intent.ACTION_VIEW, android.net.Uri.parse(link)).apply {
            setPackage(packageName)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        }

    /**
     * ntfy's view actions as buttons, at most MAX_BUTTONS, in the order they came. Like the tap, a
     * button opens its link only when the link is into this app. It opens the app straight from
     * the notification, because Android 12 and later do not let a receiver or service started by a
     * notification open an activity. So a button does not dismiss the notification, and ntfy's
     * "clear" is not honoured. ntfy's http and broadcast actions are not drawn. Each button has its
     * own request code, so no two share a PendingIntent.
     */
    private fun buttons(id: String, actions: JSONArray?): List<Pair<String, PendingIntent>> {
        val out = mutableListOf<Pair<String, PendingIntent>>()
        if (actions == null) return out
        for (i in 0 until actions.length()) {
            if (out.size == MAX_BUTTONS) break
            val a = actions.optJSONObject(i) ?: continue
            val label = a.optString("label")
            val url = a.optString("url")
            if (a.optString("action") != "view" || label.isBlank() || !intoApp(url)) continue
            out.add(label to PendingIntent.getActivity(this, "$id#$i".hashCode(), linkIntent(url), FLAGS))
        }
        return out
    }

    /** The channel for an ntfy priority: an alarm, a chime, or no sound. */
    private fun channelFor(priority: Int): String = when {
        priority >= ALARM_FROM -> ALARM_CHANNEL
        priority >= CHIME_FROM -> ALERT_CHANNEL
        else -> SILENT_CHANNEL
    }

    /** What Android before 8.0, which has no channels, goes by instead. */
    private fun priorityFor(channel: String): Int = when (channel) {
        ALARM_CHANNEL -> NotificationCompat.PRIORITY_MAX
        ALERT_CHANNEL -> NotificationCompat.PRIORITY_HIGH
        else -> NotificationCompat.PRIORITY_LOW
    }

    /** The message's first line that has text, for the folded notification. */
    private fun firstLine(body: String): String =
        body.lineSequence().map { it.trim() }.firstOrNull { it.isNotEmpty() } ?: body

    /** The first tag that starts with SUBTEXT_TAG_PREFIX, without it, for the header next to the
     *  app's name. No tag is shown when the prefix is empty. */
    private fun subText(tags: JSONArray?): String? {
        if (tags == null || SUBTEXT_TAG_PREFIX.isEmpty()) return null
        for (i in 0 until tags.length()) {
            val t = tags.optString(i)
            if (t.startsWith(SUBTEXT_TAG_PREFIX) && t.length > SUBTEXT_TAG_PREFIX.length) {
                return t.substring(SUBTEXT_TAG_PREFIX.length)
            }
        }
        return null
    }
}

/**
 * Starts the service after a reboot and after the app is updated. Installing a new version stops
 * the app and its services, and without MY_PACKAGE_REPLACED nothing would start the service again
 * until someone opened the app, so messages would stop arriving after every update.
 *
 * Some Android builds do not always deliver MY_PACKAGE_REPLACED, so the service can stay stopped
 * after an update. <package>.START_LISTENER starts it from outside without opening the app, for
 * example over adb: am broadcast -n <package>/.BootReceiver -a <package>.START_LISTENER
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED ||
            intent.action == Intent.ACTION_MY_PACKAGE_REPLACED ||
            intent.action == context.packageName + ".START_LISTENER") {
            val i = Intent(context, NtfyService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(i)
            else context.startService(i)
        }
    }
}
`;
}

const DEPS_MARKER = "expo-ntfy-deps";

/** app/build.gradle with the dependencies and the BuildConfig field. The field is replaced on every
 *  build, so building again with a different URL corrects it. */
function patchAppGradle(src, o) {
  let g = src;
  if (!g.includes(DEPS_MARKER)) {
    g = g.replace(/dependencies\s*\{/, `dependencies {
    // ${DEPS_MARKER}
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("androidx.core:core-ktx:1.13.1")`);
  }
  const field = `buildConfigField("String", "${o.buildConfigField}", "\\"${o.url}\\"")`;
  const existing = new RegExp(`buildConfigField\\("String", "${o.buildConfigField}", "\\\\"[^"\\\\]*\\\\""\\)`);
  if (existing.test(g)) {
    g = g.replace(existing, field);
  } else {
    g = g.replace(/defaultConfig\s*\{/, `defaultConfig {\n        ${field}`);
    if (/buildFeatures\s*\{/.test(g)) {
      if (!/buildConfig\s+true/.test(g)) g = g.replace(/buildFeatures\s*\{/, "buildFeatures {\n        buildConfig true");
    } else {
      g = g.replace(/android\s*\{/, "android {\n    buildFeatures { buildConfig true }");
    }
  }
  return g;
}

/** MainActivity.kt starting the service in onCreate, or the service would only run after a reboot. */
function patchMainActivity(src) {
  if (src.includes("NtfyService::class.java")) return src;
  return src.replace(/(super\.onCreate\([^)]*\))/, `$1
    // hold the ntfy socket natively (expo-ntfy): the JavaScript socket stops when the screen locks
    try {
      val svc = android.content.Intent(this, NtfyService::class.java)
      if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O)
        startForegroundService(svc) else startService(svc)
    } catch (e: Exception) { android.util.Log.w("NtfyService", "start: " + e.message) }`);
}

/** What the specialUse service is for, as Android asks every specialUse service to say. */
const SPECIAL_USE_SUBTYPE =
  "Holds the connection to a self-hosted ntfy server so its notifications arrive without Firebase";

const PERMISSIONS = [
  "android.permission.FOREGROUND_SERVICE",
  "android.permission.FOREGROUND_SERVICE_SPECIAL_USE",
  "android.permission.RECEIVE_BOOT_COMPLETED",
  "android.permission.WAKE_LOCK",
  // Android lists an app under Do Not Disturb access only when it asks for this. The person grants
  // that access there, and only then does the alarm channel keep its bypass of Do Not Disturb.
  "android.permission.ACCESS_NOTIFICATION_POLICY",
];

/** The manifest (as Expo's parsed XML object) with the permissions, the service and the receiver. */
function addManifestEntries(manifest, app) {
  manifest["uses-permission"] = manifest["uses-permission"] || [];
  for (const name of PERMISSIONS) {
    if (!manifest["uses-permission"].some((p) => p.$["android:name"] === name)) {
      manifest["uses-permission"].push({ $: { "android:name": name } });
    }
  }
  // A listener runs all day, so its type is specialUse. A dataSync service gets 6 hours in any 24
  // on Android 15: the system then stops it ("did not stop within its timeout") and refuses a new
  // start ("Time limit already exhausted for foreground service type dataSync") until the person
  // opens the app, so notifications stopped every afternoon with nothing on the phone to say so.
  // specialUse has no time limit and must state its purpose in a property. The entry is rewritten
  // rather than kept, so a build without --clean cannot carry an older type forward.
  manifest["uses-permission"] = manifest["uses-permission"].filter(
    (p) => p.$["android:name"] !== "android.permission.FOREGROUND_SERVICE_DATA_SYNC");
  app.service = (app.service || []).filter((s) => s.$["android:name"] !== ".NtfyService");
  app.service.push({
    $: {
      "android:name": ".NtfyService",
      "android:exported": "false",
      "android:foregroundServiceType": "specialUse",
    },
    property: [{
      $: {
        "android:name": "android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE",
        "android:value": SPECIAL_USE_SUBTYPE,
      },
    }],
  });
  // Rewritten like the service, so a build without --clean gets the START_LISTENER filter too.
  // ${applicationId} is filled in by Gradle's manifest merger.
  app.receiver = (app.receiver || []).filter((r) => r.$["android:name"] !== ".BootReceiver");
  app.receiver.push({
    $: { "android:name": ".BootReceiver", "android:exported": "true" },
    "intent-filter": [
      { action: [{ $: { "android:name": "android.intent.action.BOOT_COMPLETED" } }] },
      { action: [{ $: { "android:name": "android.intent.action.MY_PACKAGE_REPLACED" } }] },
      { action: [{ $: { "android:name": "${applicationId}.START_LISTENER" } }] },
    ],
  });
  return manifest;
}

function withNtfyForegroundService(config, options = {}) {
  const { withDangerousMod, withAndroidManifest, AndroidConfig } = loadConfigPlugins(config);

  config = withDangerousMod(config, [
    "android",
    (cfg) => {
      // Resolved here and not when the plugin is loaded, so that reading the app config (expo
      // start, the web build) never needs the phone's ntfy address.
      const o = resolveOptions(cfg, options);
      const root = cfg.modRequest.platformProjectRoot;
      const dir = path.join(root, "app/src/main/java", ...o.package.split("."));
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, "NtfyService.kt"), renderKotlin(o));

      const gradle = path.join(root, "app/build.gradle");
      fs.writeFileSync(gradle, patchAppGradle(fs.readFileSync(gradle, "utf8"), o));

      const mainActivity = path.join(dir, "MainActivity.kt");
      if (fs.existsSync(mainActivity)) {
        fs.writeFileSync(mainActivity, patchMainActivity(fs.readFileSync(mainActivity, "utf8")));
      }
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

module.exports = withNtfyForegroundService;
module.exports.withNtfyForegroundService = withNtfyForegroundService;
module.exports.resolveOptions = resolveOptions;
module.exports.checkTopicUrl = checkTopicUrl;
module.exports.isLoopbackHost = isLoopbackHost;
module.exports.renderKotlin = renderKotlin;
module.exports.patchAppGradle = patchAppGradle;
module.exports.patchMainActivity = patchMainActivity;
module.exports.addManifestEntries = addManifestEntries;
