import { FlatList, Pressable, RefreshControl, Text, View } from "react-native";
import type { PendingTask } from "@drkostas/claude-human-client";

import { client } from "../config";
import { pendingLine } from "../logic";
import { s } from "../styles";
import { useLoad } from "../useLoad";

export function PendingScreen({ onOpen, refreshKey }: { onOpen: (id: string) => void; refreshKey: number }) {
  const { data, error, loading, reload } = useLoad<PendingTask[]>(
    client ? () => client!.pending() : null,
    [refreshKey],
  );
  return (
    <FlatList
      style={s.screen}
      contentContainerStyle={s.pad}
      data={data ?? []}
      keyExtractor={(t) => t.intent}
      refreshControl={<RefreshControl refreshing={loading} onRefresh={reload} />}
      ListHeaderComponent={
        <View style={{ gap: 4 }}>
          <Text style={s.h1}>Waiting on you</Text>
          {error ? <Text style={s.error}>{error}</Text> : null}
        </View>
      }
      ListEmptyComponent={!loading && !error ? <Text style={s.muted}>Nothing is waiting on you.</Text> : null}
      renderItem={({ item }) => (
        <Pressable style={s.row} onPress={() => onOpen(item.intent)} accessibilityRole="button">
          <Text style={s.body}>{item.reason || item.verb}</Text>
          <Text style={s.muted}>{pendingLine(item)}</Text>
        </Pressable>
      )}
    />
  );
}
