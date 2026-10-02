import { StyleSheet } from "react-native";

export const s = StyleSheet.create({
  screen: { flex: 1, backgroundColor: "#fafafa" },
  pad: { padding: 16, gap: 12 },
  h1: { fontSize: 22, fontWeight: "600", color: "#111" },
  h2: { fontSize: 13, fontWeight: "600", color: "#666", textTransform: "uppercase", marginTop: 8 },
  body: { fontSize: 16, color: "#222", lineHeight: 22 },
  muted: { fontSize: 14, color: "#666" },
  error: { fontSize: 14, color: "#b00020" },
  row: { backgroundColor: "#fff", borderRadius: 10, padding: 14, gap: 4, borderWidth: 1, borderColor: "#e5e5e5" },
  rowHead: { borderColor: "#2a6df4", borderWidth: 2 },
  button: { backgroundColor: "#2a6df4", borderRadius: 10, paddingVertical: 14, alignItems: "center" },
  buttonText: { color: "#fff", fontSize: 16, fontWeight: "600" },
  link: { color: "#2a6df4", fontSize: 16 },
  tabs: { flexDirection: "row", borderTopWidth: 1, borderColor: "#e5e5e5", backgroundColor: "#fff" },
  tab: { flex: 1, paddingVertical: 14, alignItems: "center" },
  tabOn: { fontWeight: "700", color: "#111" },
  tabOff: { color: "#666" },
  done: { color: "#1b7f3b", fontSize: 16 },
  waiting: { color: "#8a5a00", fontSize: 16 },
});
