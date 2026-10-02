/** Where the task server and the ntfy topic are. Both come from the build's environment, so
 *  nothing in the code names a host. See .env.example. */
import { Platform } from "react-native";
import { createTaskClient } from "@drkostas/claude-human-client";

import { SUPPORTS } from "./logic";

export const TASKS_URL = process.env.EXPO_PUBLIC_TASKS_URL ?? "";
export const NTFY_URL = process.env.EXPO_PUBLIC_NTFY_URL || undefined;
export const LINK_PREFIX = "expotasks://";

/** null when no server is configured, so the app can say so instead of failing on every call. */
export const client = TASKS_URL
  ? createTaskClient({
      baseUrl: TASKS_URL,
      token: process.env.EXPO_PUBLIC_TASKS_TOKEN,
      supports: SUPPORTS,
      platform: Platform.OS,
      retries: 1,
    })
  : null;
