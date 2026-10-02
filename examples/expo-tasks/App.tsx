import { useCallback, useState } from "react";
import { Pressable, SafeAreaView, Text, View } from "react-native";
import { StatusBar } from "expo-status-bar";
import { installForegroundHandler, useNotificationResponses, useNtfy } from "@drkostas/expo-ntfy/notify";

import { LINK_PREFIX, NTFY_URL } from "./src/config";
import { routeFor, type Route } from "./src/logic";
import { HistoryScreen } from "./src/screens/HistoryScreen";
import { PendingScreen } from "./src/screens/PendingScreen";
import { TaskScreen } from "./src/screens/TaskScreen";
import { s } from "./src/styles";

installForegroundHandler();

export default function App() {
  const [route, setRoute] = useState<Route>({ name: "pending" });
  const [refreshKey, setRefreshKey] = useState(0);

  // a new task announced on the topic refreshes the list while the app is open
  useNtfy(NTFY_URL, () => setRefreshKey((n) => n + 1), {
    text: { appName: "Tasks", linkPrefix: LINK_PREFIX },
  });

  // a tap on a notification, or a link that opened the app, goes to its task
  const go = useCallback((url: string | undefined) => {
    const r = routeFor(url, LINK_PREFIX);
    if (r) setRoute(r);
  }, []);
  useNotificationResponses({ onTap: go, onLink: go });

  return (
    <SafeAreaView style={s.screen}>
      <StatusBar style="dark" />
      <View style={{ flex: 1 }}>
        {route.name === "task" ? (
          <TaskScreen id={route.id} onBack={() => setRoute({ name: "pending" })} />
        ) : route.name === "history" ? (
          <HistoryScreen />
        ) : (
          <PendingScreen refreshKey={refreshKey} onOpen={(id) => setRoute({ name: "task", id })} />
        )}
      </View>
      <View style={s.tabs}>
        {(["pending", "history"] as const).map((name) => (
          <Pressable key={name} style={s.tab} onPress={() => setRoute(name === "pending" ? { name: "pending" } : { name: "history" })} accessibilityRole="tab">
            <Text style={route.name === name ? s.tabOn : s.tabOff}>
              {name === "pending" ? "Waiting" : "History"}
            </Text>
          </Pressable>
        ))}
      </View>
    </SafeAreaView>
  );
}
