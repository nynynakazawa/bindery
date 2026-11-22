import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { t } from "../copy";
import type { BinderySnapshot, SyncInfo } from "../types";
import { syncPresentation } from "./DashboardParts";
import { SyncView } from "./OperationsViews";
import { OverviewView } from "./OverviewView";

const sync: SyncInfo = {
  configured: true, remote: null, machine: null, sync_id: null, last_success: null, last_attempt: null,
  git: "ok", index: "never", last_error: null, blocked_reason: null, conflicts: 0, held_secrets: 0,
  scheduler: "absent", interval_seconds: null, peers: [], error: null,
};

function snapshot(overrides: Partial<SyncInfo>): BinderySnapshot {
  return {
    status: {
      version: "0.1.0", vault: "/test/vault", vault_exists: true, indexed: false,
      notes: null, passages: null, embedded: null, semantic: "off", state_dir: "/test/state", error: null,
    },
    clients: { clients: [], connections: [], error: null },
    sync: { ...sync, ...overrides },
    syncing: false, refreshed_at: "",
  };
}

function renderSync(overrides: Partial<SyncInfo>) {
  return renderToStaticMarkup(<SyncView syncing={false} retry={() => {}} refresh={() => {}}
    goSettings={() => {}} open={() => {}} run={async () => {}} snapshot={snapshot(overrides)} />);
}

it("offers to open a non-Markdown review file in the viewer", () => {
  const output = renderSync({ held_secrets: 1, held_paths: ["drafts/local.env"] });
  expect(output).toMatch(/<button[^>]*class="[^"]*attention-file[^"]*"[^>]*>[^<]*(?:<span[^>]*>[^<]*<\/span>)*<span[^>]*>drafts\/local\.env<\/span><\/button>/u);
});

it("shows synced secret notes as viewer buttons without an attention badge", () => {
  const warnings = { secret_count: 1, secret_paths: ["drafts/synced.md"] };
  const output = renderSync(warnings);
  expect(output).toContain(t("sync.syncedSecrets"));
  expect(output).toContain(t("sync.noAttention"));
  expect(output).toMatch(/<button[^>]*sync-secret-note[^>]*><span[^>]*>drafts\/synced\.md<\/span><\/button>/u);
  expect(syncPresentation({ ...sync, ...warnings }, false).attention).toBeFalsy();
});

it("lists held secrets only in attention and keeps conflict and stop indicators", () => {
  const held = { held_secrets: 1, held_paths: ["drafts/held.md"] };
  const output = renderSync({ ...held, secret_count: 1, secret_paths: held.held_paths });
  expect(output).toContain(t("sync.heldSecrets"));
  expect(output).toContain("drafts/held.md");
  expect(output).not.toContain(t("sync.syncedSecrets"));
  expect(syncPresentation({ ...sync, ...held }, false).attention).toBe(true);
  expect(syncPresentation({ ...sync, conflict_paths: ["drafts/copy.md"] }, false).attention).toBe(true);
  expect(syncPresentation({ ...sync, git: "blocked" }, false).attention).toBe(true);
});

it("gives the overview an attention badge only when there is work to review", () => {
  const list = { loading: false, refresh: async () => {} };
  const warnings = { secret_count: 1, secret_paths: ["drafts/synced.md"] };
  const overview = (overrides: Partial<SyncInfo>) => renderToStaticMarkup(
    <OverviewView snapshot={snapshot({ ...warnings, ...overrides })} rankings={list} recent={list}
      syncing={false} go={() => {}} open={() => {}} refresh={() => {}} />,
  );
  expect(overview({})).not.toContain("has-attention");
  expect(overview({ held_secrets: 1 })).toContain("has-attention");
});
