import { afterEach, describe, expect, it, vi } from "vitest";
import type { BinderyApi } from "../types";
import { createSearch } from "./search";

type Response = Awaited<ReturnType<BinderyApi["search"]>>;

function pendingResponse() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("debounced note search", () => {
  afterEach(() => { vi.useRealTimers(); });

  it("starts only the latest query after 250ms of input inactivity", async () => {
    vi.useFakeTimers();
    const search = vi.fn<BinderyApi["search"]>().mockResolvedValue({ error: null, results: [] });
    const controller = createSearch(search, vi.fn());
    controller.run("first");
    await vi.advanceTimersByTimeAsync(249);
    controller.run("second");
    await vi.advanceTimersByTimeAsync(249);
    expect(search).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(search).toHaveBeenCalledExactlyOnceWith("second");
  });

  it("discards earlier responses as soon as new input arrives, even before its request starts", async () => {
    vi.useFakeTimers();
    const first = pendingResponse();
    const second = pendingResponse();
    const search = vi.fn<BinderyApi["search"]>().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const publish = vi.fn();
    const controller = createSearch(search, publish);
    controller.run("first", 0);
    controller.run("second");
    first.resolve({ error: "Outdated" });
    await vi.advanceTimersByTimeAsync(249);
    expect(publish).toHaveBeenLastCalledWith({ kind: "loading" });
    await vi.advanceTimersByTimeAsync(1);
    second.resolve({ error: null, results: [] });
    await vi.advanceTimersByTimeAsync(0);
    expect(publish).toHaveBeenLastCalledWith({ kind: "ready", results: [] });
  });

  it("keeps the newest results when the old request finishes last", async () => {
    const first = pendingResponse();
    const second = pendingResponse();
    const search = vi.fn<BinderyApi["search"]>().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const publish = vi.fn();
    const controller = createSearch(search, publish);
    controller.run("first", 0);
    controller.run("second", 0);
    const result = { path: "second.md", heading: null, snippet: "Second", match: "keyword", score: 1 };
    second.resolve({ error: null, results: [result] });
    await second.promise;
    first.resolve({ error: null, results: [] });
    await first.promise;
    expect(publish).toHaveBeenLastCalledWith({ kind: "ready", results: [result] });
  });

  it("does not repopulate results after clearing a query", async () => {
    const inflight = pendingResponse();
    const publish = vi.fn();
    const controller = createSearch(() => inflight.promise, publish);
    controller.run("old", 0);
    controller.run("");
    inflight.resolve({ error: null, results: [] });
    await inflight.promise;
    expect(publish).toHaveBeenLastCalledWith({ kind: "idle" });
  });
});
