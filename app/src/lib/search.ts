import { useEffect, useMemo, useState } from "react";
import type { BinderyApi, SearchEntry } from "../types";

type SearchState =
  | { kind: "idle" | "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; results: SearchEntry[] };

export function createSearch(search: BinderyApi["search"], publish: (state: SearchState) => void) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let generation = 0;

  function cancel() {
    generation++;
    clearTimeout(timer);
  }

  return {
    cancel,
    run(query: string, delay = 250) {
      cancel();
      if (!query) {
        publish({ kind: "idle" });
        return;
      }
      publish({ kind: "loading" });
      const current = generation;
      const execute = async () => {
        try {
          const response = await search(query);
          if (generation !== current) return;
          publish(response.error === null
            ? { kind: "ready", results: response.results }
            : { kind: "error", message: response.error });
        } catch (error) {
          if (generation === current) publish({ kind: "error", message: error instanceof Error ? error.message : String(error) });
        }
      };
      if (delay === 0) void execute();
      else timer = setTimeout(() => void execute(), delay);
    },
  };
}

export function useNoteSearch(search: BinderyApi["search"]) {
  const [state, setState] = useState<SearchState>({ kind: "idle" });
  const controller = useMemo(() => createSearch(search, setState), [search]);
  useEffect(() => controller.cancel, [controller]);
  return { state, run: controller.run };
}
