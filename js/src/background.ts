/** Read the topic while the app is not open, on a schedule the OS controls.
 *
 *  The foreground socket in notify.ts stops when the app is backgrounded. This registers a
 *  background task that runs `pollTopicOnce` (poll.ts) on each wake. The interval is a request to
 *  the OS, not a promise: Android decides, and aggressive battery managers can defer it for a long
 *  time. It is a floor under the live socket and the native service, not a replacement for them.
 *
 *  `defineTopicPollTask` must run at module top level (Expo's TaskManager requires tasks to be
 *  defined in the global scope), so call it from a module the app imports at startup. */
import * as BackgroundTask from "expo-background-task";
import * as TaskManager from "expo-task-manager";
import { Platform } from "react-native";

/** Define the task `name` to run `pass` on every OS wake. */
export function defineTopicPollTask(name: string, pass: () => Promise<unknown>): void {
  TaskManager.defineTask(name, async () => {
    try {
      await pass();
      return BackgroundTask.BackgroundTaskResult.Success;
    } catch {
      return BackgroundTask.BackgroundTaskResult.Failed;
    }
  });
}

/** Ask the OS to wake the task about every `minutes`. False on web, or when registration fails. */
export async function registerTopicPoll(name: string, minutes = 15): Promise<boolean> {
  if (Platform.OS === "web") return false;
  try {
    if (!(await TaskManager.isTaskRegisteredAsync(name))) {
      await BackgroundTask.registerTaskAsync(name, { minimumInterval: minutes });
    }
    return true;
  } catch {
    return false;
  }
}
