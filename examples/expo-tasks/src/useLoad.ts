import { useCallback, useEffect, useState } from "react";

/** Load something once, and again on `reload`. Keeps the last good data while a reload runs. */
export function useLoad<T>(load: (() => Promise<T>) | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!load) {
      setLoading(false);
      setError("No task server is configured. Set EXPO_PUBLIC_TASKS_URL.");
      return;
    }
    let alive = true;
    setLoading(true);
    load()
      .then((d) => {
        if (!alive) return;
        setData(d);
        setError(null);
      })
      .catch((e) => alive && setError(String(e instanceof Error ? e.message : e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, ...deps]);
  const reload = useCallback(() => setTick((n) => n + 1), []);
  return { data, error, loading, reload };
}
