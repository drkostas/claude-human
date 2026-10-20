/**
 * Expo config plugin that turns an Android phone's wireless debugging back on after a restart.
 *
 * `adb tcpip 5555` lets a phone listen for adb on every network until it restarts. After a restart
 * only adbd itself can listen on that port again, and a machine that is not on the phone's cable or
 * Wi-Fi can reach adbd only through wireless debugging. Android switches wireless debugging off at
 * every restart (Wi-Fi connects after adbd starts, which Android reads as a change of network), so
 * after a restart nothing remote can get in.
 *
 * This plugin writes a small Kotlin job that runs after a restart, after an update of the app, and
 * every `intervalMinutes` while the phone is on an unmetered network. It turns wireless debugging
 * on when all of these hold:
 *   - the app holds WRITE_SECURE_SETTINGS (granted once over adb:
 *     `adb shell pm grant <package> android.permission.WRITE_SECURE_SETTINGS`)
 *   - USB debugging is on in Developer options
 *   - wireless debugging is off, and nothing listens on 127.0.0.1:<fixedPort>
 *   - the phone is on Wi-Fi, and it was not tried on this Wi-Fi in the last `retryMinutes`
 * A machine that reaches the phone (over a VPN, say) can then find the wireless-debugging port,
 * connect and run `adb tcpip <fixedPort>` again (claude-ops `adb-reach` does exactly that).
 *
 * On a Wi-Fi network the phone does not trust yet, Android answers with its own prompt ("Allow
 * wireless debugging on this network?") and leaves the setting off until the person allows it.
 * The job never presses it. It tries at most once per network per `retryMinutes`, so the prompt
 * does not come back every few minutes. On mobile data alone nothing can turn wireless debugging
 * on: Android allows it only on Wi-Fi.
 *
 * Options (all optional):
 *   package          Kotlin package (default: expo.android.package)
 *   fixedPort        the port `adb tcpip` listens on (default 5555)
 *   intervalMinutes  how often the job runs while on an unmetered network (default 15, Android's
 *                    shortest period for a periodic job)
 *   retryMinutes     how long before the job tries again on the same Wi-Fi network (default 60)
 *   jobId            the first of the two JobScheduler ids the job uses (default 41550)
 *   logTag           Android log tag (default "AdbKeeper")
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
  throw new Error("expo-adb-keeper: cannot find expo/config-plugins. Install expo in the app.");
}

function whole(value, name, min, max) {
  const n = Number(value);
  if (!Number.isInteger(n) || n < min || n > max) {
    throw new Error(`expo-adb-keeper: ${name} must be a whole number from ${min} to ${max}, not ${value}`);
  }
  return n;
}

/** The options with their defaults, checked. Throws on a value that cannot work. */
function resolveOptions(config, options = {}) {
  const pkg = options.package || (config.android && config.android.package);
  if (!pkg || !/^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$/.test(pkg)) {
    throw new Error("expo-adb-keeper: set expo.android.package (or the package option) to a Java package name");
  }
  return {
    package: pkg,
    fixedPort: whole(options.fixedPort ?? 5555, "fixedPort", 1, 65535),
    // JobScheduler runs a periodic job at most every 15 minutes
    intervalMinutes: whole(options.intervalMinutes ?? 15, "intervalMinutes", 15, 24 * 60),
    retryMinutes: whole(options.retryMinutes ?? 60, "retryMinutes", 1, 7 * 24 * 60),
    jobId: whole(options.jobId ?? 41550, "jobId", 1, 2147483646),
    logTag: String(options.logTag || "AdbKeeper").slice(0, 23),
  };
}

function renderKotlin(o) {
  return `package ${o.package}

import android.app.job.JobInfo
import android.app.job.JobParameters
import android.app.job.JobScheduler
import android.app.job.JobService
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.provider.Settings
import android.util.Log
import java.net.InetSocketAddress
import java.net.Socket

/**
 * Turns wireless debugging back on after a restart, so a machine that reaches the phone can bring
 * adb back on port ${o.fixedPort}. Written by @drkostas/expo-adb-keeper; edit the plugin's options, not
 * this file.
 */
object AdbKeeper {
    const val TAG = "${o.logTag}"
    const val FIXED_PORT = ${o.fixedPort}
    private const val SOON_ID = ${o.jobId}
    private const val PERIODIC_ID = ${o.jobId + 1}
    private const val INTERVAL_MS = ${o.intervalMinutes}L * 60_000L
    private const val RETRY_MS = ${o.retryMinutes}L * 60_000L
    private const val PREFS = "expo_adb_keeper"
    private const val ADB_WIFI = "adb_wifi_enabled"

    /** A run as soon as the phone is on an unmetered network, and one every interval after it. */
    fun schedule(context: Context) {
        val jobs = context.getSystemService(JobScheduler::class.java) ?: return
        val job = ComponentName(context, AdbKeeperJob::class.java)
        jobs.schedule(JobInfo.Builder(SOON_ID, job)
            .setRequiredNetworkType(JobInfo.NETWORK_TYPE_UNMETERED).build())
        if (jobs.getPendingJob(PERIODIC_ID) == null) {
            jobs.schedule(JobInfo.Builder(PERIODIC_ID, job)
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_UNMETERED)
                .setPeriodic(INTERVAL_MS)
                .setPersisted(true).build())
        }
    }

    private fun listening(port: Int): Boolean = try {
        Socket().use { it.connect(InetSocketAddress("127.0.0.1", port), 500); true }
    } catch (e: Exception) {
        false
    }

    /** One check; says what it did, for the log. */
    fun check(context: Context): String {
        if (context.checkSelfPermission("android.permission.WRITE_SECURE_SETTINGS")
                != PackageManager.PERMISSION_GRANTED) {
            return "no WRITE_SECURE_SETTINGS: grant it once with adb shell pm grant"
        }
        val settings = context.contentResolver
        if (Settings.Global.getInt(settings, Settings.Global.ADB_ENABLED, 0) != 1) return "USB debugging is off"
        if (Settings.Global.getInt(settings, ADB_WIFI, 0) == 1) return "wireless debugging is on"
        if (listening(FIXED_PORT)) return "port $FIXED_PORT listens"
        val net = context.getSystemService(ConnectivityManager::class.java) ?: return "no connectivity service"
        // the active network is the VPN when one runs, so look for the Wi-Fi network itself
        @Suppress("DEPRECATION")
        val wifi = net.allNetworks.firstOrNull {
            net.getNetworkCapabilities(it)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
        } ?: return "not on Wi-Fi"
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val now = System.currentTimeMillis()
        if (prefs.getString("network", null) == wifi.toString() && now - prefs.getLong("at", 0L) < RETRY_MS) {
            return "tried on this Wi-Fi less than ${o.retryMinutes} minutes ago"
        }
        prefs.edit().putString("network", wifi.toString()).putLong("at", now).apply()
        Settings.Global.putInt(settings, ADB_WIFI, 1)
        return "turned wireless debugging on"
    }
}

class AdbKeeperJob : JobService() {
    override fun onStartJob(params: JobParameters): Boolean {
        Thread {
            val said = try { AdbKeeper.check(applicationContext) } catch (e: Exception) { "failed: " + e.message }
            Log.i(AdbKeeper.TAG, said)
            jobFinished(params, false)
        }.start()
        return true
    }

    override fun onStopJob(params: JobParameters): Boolean = false
}

class AdbKeeperReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED || intent.action == Intent.ACTION_MY_PACKAGE_REPLACED) {
            AdbKeeper.schedule(context)
        }
    }
}
`;
}

const PERMISSIONS = [
  "android.permission.RECEIVE_BOOT_COMPLETED",
  "android.permission.ACCESS_NETWORK_STATE",
];
const SECURE = "android.permission.WRITE_SECURE_SETTINGS";

/** The manifest (as Expo's parsed XML object) with the permissions, the job and the receiver. */
function addManifestEntries(manifest, app) {
  manifest.$ = manifest.$ || {};
  manifest.$["xmlns:tools"] = manifest.$["xmlns:tools"] || "http://schemas.android.com/tools";
  manifest["uses-permission"] = manifest["uses-permission"] || [];
  for (const name of PERMISSIONS) {
    if (!manifest["uses-permission"].some((p) => p.$["android:name"] === name)) {
      manifest["uses-permission"].push({ $: { "android:name": name } });
    }
  }
  // a system permission an app cannot be granted at install; adb grants it, and lint must be told
  manifest["uses-permission"] = manifest["uses-permission"].filter((p) => p.$["android:name"] !== SECURE);
  manifest["uses-permission"].push({ $: { "android:name": SECURE, "tools:ignore": "ProtectedPermissions" } });
  // rewritten rather than kept, so a build without --clean never carries an older entry forward
  app.service = (app.service || []).filter((s) => s.$["android:name"] !== ".AdbKeeperJob");
  app.service.push({
    $: {
      "android:name": ".AdbKeeperJob",
      "android:exported": "false",
      "android:permission": "android.permission.BIND_JOB_SERVICE",
    },
  });
  app.receiver = (app.receiver || []).filter((r) => r.$["android:name"] !== ".AdbKeeperReceiver");
  app.receiver.push({
    $: { "android:name": ".AdbKeeperReceiver", "android:exported": "true" },
    "intent-filter": [
      { action: [{ $: { "android:name": "android.intent.action.BOOT_COMPLETED" } }] },
      { action: [{ $: { "android:name": "android.intent.action.MY_PACKAGE_REPLACED" } }] },
    ],
  });
  return manifest;
}

function withAdbKeeper(config, options = {}) {
  const { withDangerousMod, withAndroidManifest, AndroidConfig } = loadConfigPlugins(config);
  config = withDangerousMod(config, [
    "android",
    (cfg) => {
      const o = resolveOptions(cfg, options);
      const dir = path.join(cfg.modRequest.platformProjectRoot, "app/src/main/java", ...o.package.split("."));
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, "AdbKeeper.kt"), renderKotlin(o));
      return cfg;
    },
  ]);
  config = withAndroidManifest(config, (cfg) => {
    resolveOptions(cfg, options);
    const app = AndroidConfig.Manifest.getMainApplicationOrThrow(cfg.modResults);
    addManifestEntries(cfg.modResults.manifest, app);
    return cfg;
  });
  return config;
}

module.exports = withAdbKeeper;
module.exports.withAdbKeeper = withAdbKeeper;
module.exports.resolveOptions = resolveOptions;
module.exports.renderKotlin = renderKotlin;
module.exports.addManifestEntries = addManifestEntries;
