import { useState } from "react";
import { Linking, Pressable, ScrollView, Text, View } from "react-native";
import type { Handoff, PendingTask } from "@drkostas/claude-human-client";

import { client } from "../config";
import { chainRows, doneMessage, openProblem, taskSections } from "../logic";
import { s } from "../styles";
import { useLoad } from "../useLoad";

export function TaskScreen({ id, onBack }: { id: string; onBack: () => void }) {
  const { data: task, error, reload } = useLoad<PendingTask>(client ? () => client!.task(id) : null, [id]);
  const [answer, setAnswer] = useState<{ tone: "done" | "waiting"; text: string } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function open(h: Handoff) {
    if (!client) return;
    setProblem(null);
    try {
      // the server readies the head of the chain; the app opens only what it said is ready
      const r = await client.open(id);
      const why = openProblem(r);
      if (why) setProblem(why);
      else await Linking.openURL(r.target ?? h.target ?? "");
    } catch (e) {
      setProblem(String(e instanceof Error ? e.message : e));
    }
  }

  async function check() {
    if (!client) return;
    setBusy(true);
    try {
      const r = await client.done(id);
      setAnswer(doneMessage(r));
      if (r.done) reload();
    } catch (e) {
      setAnswer({ tone: "waiting", text: String(e instanceof Error ? e.message : e) });
    } finally {
      setBusy(false);
    }
  }

  if (!task) {
    return (
      <View style={[s.screen, s.pad]}>
        <Pressable onPress={onBack}><Text style={s.link}>Back</Text></Pressable>
        <Text style={error ? s.error : s.muted}>{error ?? "Loading"}</Text>
      </View>
    );
  }

  const sec = taskSections(task);
  const closed = task.state && task.state !== "open";
  return (
    <ScrollView style={s.screen} contentContainerStyle={s.pad}>
      <Pressable onPress={onBack}><Text style={s.link}>Back</Text></Pressable>
      <Text style={s.h1}>{sec.title}</Text>

      <Text style={s.h2}>Why you</Text>
      <Text style={s.body}>{sec.whyYou}</Text>

      <Text style={s.h2}>What to do</Text>
      <Text style={s.body}>{sec.whatToDo}</Text>

      <Text style={s.h2}>Ways to reach it</Text>
      {chainRows(task).map((row, i) => (
        <View key={row.key} style={[s.row, row.isHead ? s.rowHead : null]}>
          <Text style={s.body}>{row.title}{row.isHead ? " (start here)" : ""}</Text>
          <Text style={s.muted} numberOfLines={row.isFloor ? undefined : 2}>{row.detail}</Text>
          {row.openable && !closed ? (
            <Pressable onPress={() => open(task.handoffs[i])} accessibilityRole="button">
              <Text style={s.link}>Open</Text>
            </Pressable>
          ) : null}
        </View>
      ))}
      {problem ? <Text style={s.error}>{problem}</Text> : null}

      {closed ? (
        <Text style={s.muted}>This task is closed ({task.outcome ?? task.state}).</Text>
      ) : (
        <Pressable style={s.button} onPress={check} disabled={busy} accessibilityRole="button">
          <Text style={s.buttonText}>{busy ? "Checking" : "I've done it, check"}</Text>
        </Pressable>
      )}
      {answer ? <Text style={answer.tone === "done" ? s.done : s.waiting}>{answer.text}</Text> : null}
    </ScrollView>
  );
}
