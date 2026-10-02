/** Expo and React Native glue that turns an ntfy topic into notifications on this device, without
 *  Firebase. The app holds its own WebSocket to the topic while it is open and raises a local
 *  notification per message. The wire logic is in proto.ts.
 *
 *  On Android the config plugin adds a native foreground service that holds the socket while the
 *  app is closed and posts to the tray itself. Only one part of the app may own the tray, or every
 *  message appears twice, so on Android this module stays out of the tray by default (see
 *  TRAY_IS_NATIVE). */
import { useEffect, useRef, useState } from "react";
import { Linking, Platform } from "react-native";
import * as Notifications from "expo-notifications";

import { frameToNotification, parseFrame, wsUrlFrom, type NotificationText, type NtfyFrame } from "./proto";

/** True on Android, where the plugin's native service posts every message. Set the `trayIsNative`
 *  option to false if the app does not use the config plugin. */
export const TRAY_IS_NATIVE = Platform.OS === "android";

/** Show notifications that arrive while the app is in the foreground. Call once at startup. */
export function installForegroundHandler(): void {
  if (Platform.OS === "web") return;
  Notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowBanner: true,
      shouldShowList: true,
      shouldPlaySound: true,
      shouldSetBadge: false,
    }),
  });
}

/** Ask for notification permission once. Always false on web, which has no OS tray here. */
export async function ensureNotificationPermission(): Promise<boolean> {
  if (Platform.OS === "web") return false;
  const cur = await Notifications.getPermissionsAsync();
  if (cur.granted) return true;
  const req = await Notifications.requestPermissionsAsync();
  return req.granted;
}

/** Raise a local notification now. `url` rides in its data, so a tap can open it. Does nothing on
 *  web, and nothing on Android while the native service owns the tray. */
export async function raiseLocalNotification(
  title: string,
  body: string,
  url?: string,
  opts: { trayIsNative?: boolean } = {},
): Promise<void> {
  if (Platform.OS === "web") return;
  if (opts.trayIsNative ?? TRAY_IS_NATIVE) return;
  await Notifications.scheduleNotificationAsync({
    content: { title, body, data: url ? { url } : {} },
    trigger: null,
  });
}

export interface ResponseHandlers {
  /** a notification raised by this module was tapped. `url` is its click URL, if it had one. */
  onTap?: (url: string | undefined) => void;
  /** the app was opened by a link (the native service opens the app with the click URL as a deep
   *  link), including the link that launched it from cold */
  onLink?: (url: string) => void;
}

/** Listen for taps on notifications and for incoming links, including the ones that launched the
 *  app from cold, which are read once at startup. */
export function useNotificationResponses(handlers: ResponseHandlers): void {
  const ref = useRef(handlers);
  useEffect(() => {
    ref.current = handlers;
  }, [handlers]);
  useEffect(() => {
    if (Platform.OS === "web") return;
    let alive = true;
    const handle = (resp: Notifications.NotificationResponse | null) => {
      if (!alive || !resp) return;
      ref.current.onTap?.(resp.notification.request.content.data?.url as string | undefined);
    };
    void Notifications.getLastNotificationResponseAsync().then(handle);
    const sub = Notifications.addNotificationResponseReceivedListener(handle);
    void Linking.getInitialURL().then((u) => {
      if (alive && u) ref.current.onLink?.(u);
    });
    const link = Linking.addEventListener("url", (e) => {
      if (alive) ref.current.onLink?.(e.url);
    });
    return () => {
      alive = false;
      sub.remove();
      link.remove();
    };
  }, []);
}

export interface NtfyState {
  connected: boolean;
  last: NtfyFrame | null;
}

export interface UseNtfyOptions {
  /** how a frame reads as a notification */
  text?: NotificationText;
  /** see raiseLocalNotification */
  trayIsNative?: boolean;
  /** delay before reconnecting a dropped socket, in ms (default 3000) */
  retryMs?: number;
}

/** Hold the topic's WebSocket open while the component is mounted, reconnecting when it drops.
 *  Each message raises a local notification and calls `onMessage`. With no `topicUrl` nothing
 *  connects.
 *
 *  This socket lives only while the app is in the foreground. Android suspends it seconds after
 *  the app is backgrounded, which is why the native service and the background poll exist. */
export function useNtfy(
  topicUrl: string | undefined,
  onMessage?: (f: NtfyFrame) => void,
  opts: UseNtfyOptions = {},
): NtfyState {
  const [connected, setConnected] = useState(false);
  const [last, setLast] = useState<NtfyFrame | null>(null);
  const cb = useRef(onMessage);
  const options = useRef(opts);
  useEffect(() => {
    cb.current = onMessage;
    options.current = opts;
  });

  useEffect(() => {
    if (!topicUrl) return;
    let closed = false;
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;

    void ensureNotificationPermission();

    const connect = () => {
      if (closed) return;
      ws = new WebSocket(wsUrlFrom(topicUrl));
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!closed) retry = setTimeout(connect, options.current.retryMs ?? 3000);
      };
      ws.onerror = () => {
        try {
          ws?.close();
        } catch {
          // onclose reconnects
        }
      };
      ws.onmessage = (ev) => {
        const raw = (ev as { data?: unknown }).data;
        const frame = parseFrame(typeof raw === "string" ? raw : "");
        if (!frame) return;
        setLast(frame);
        const n = frameToNotification(frame, options.current.text);
        void raiseLocalNotification(n.title, n.body, n.url, { trayIsNative: options.current.trayIsNative });
        cb.current?.(frame);
      };
    };

    connect();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      try {
        ws?.close();
      } catch {
        // unmounting
      }
    };
  }, [topicUrl]);

  return { connected, last };
}
