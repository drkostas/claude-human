import { FlatList, RefreshControl, Text, View } from "react-native";
import type { HistoryItem } from "@drkostas/claude-human-client";

import { client } from "../config";
import { historyLine } from "../logic";
import { s } from "../styles";
import { useLoad } from "../useLoad";

export function HistoryScreen() {
  const { data, error, loading, reload } = useLoad<HistoryItem[]>(client ? () => client!.history(100) : null);
  return (
    <FlatList
      style={s.screen}
      contentContainerStyle={s.pad}
      data={data ?? []}
      keyExtractor={(h, i) => String(h.id ?? i)}
      refreshControl={<RefreshControl refreshing={loading} onRefresh={reload} />}
      ListHeaderComponent={
        <View style={{ gap: 4 }}>
          <Text style={s.h1}>History</Text>
          {error ? <Text style={s.error}>{error}</Text> : null}
        </View>
      }
      renderItem={({ item }) => {
        const line = historyLine(item);
        return (
          <View style={s.row}>
            <Text style={s.body}>{line.title}: {line.subject}</Text>
            {line.says ? <Text style={s.body}>{line.says}</Text> : null}
            <Text style={s.muted}>{item.at}</Text>
          </View>
        );
      }}
    />
  );
}
