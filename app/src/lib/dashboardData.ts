import { useCallback, useRef, useState } from "react";

const CACHE_TTL_MS = 30_000;

export function useDashboardData<T>(load: () => Promise<T>) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const updated = useRef(0);
  const pending = useRef<Promise<void> | null>(null);

  const replace = useCallback((value: T) => {
    pending.current = null;
    updated.current = Date.now();
    setData(value);
    setError(undefined);
    setLoading(false);
  }, []);

  const refresh = useCallback((force = false, supersede = false): Promise<void> => {
    if (pending.current && !supersede) return pending.current;
    if (!force && Date.now() - updated.current < CACHE_TTL_MS) return Promise.resolve();
    setLoading(true);
    const request = Promise.resolve().then(load).then((value) => {
      if (pending.current !== request) return;
      // Keep a previously loaded list visible when its background refresh fails.
      const failure = value && typeof value === "object" && "error" in value && typeof value.error === "string" ? value.error : undefined;
      if (failure) {
        setError(failure);
        return;
      }
      replace(value);
    }).catch((reason: unknown) => {
      if (pending.current === request) setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => {
      if (pending.current === request) {
        pending.current = null;
        setLoading(false);
      }
    });
    pending.current = request;
    return request;
  }, [load, replace]);

  return { data, error, loading, refresh, replace };
}

export interface ListState<T> {
  data?: T;
  error?: string;
  loading: boolean;
  refresh: (force?: boolean) => Promise<void>;
}
