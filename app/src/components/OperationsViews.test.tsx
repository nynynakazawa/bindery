import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { SyncView } from "./OperationsViews";

it("offers to open a non-Markdown review file in the viewer", () => {
  const output = renderToStaticMarkup(<SyncView syncing={false} retry={() => {}} refresh={() => {}}
    goSettings={() => {}} open={() => {}} run={async () => {}} snapshot={{
      status: {
        version: "0.1.0", vault: "/test/vault", vault_exists: true, indexed: false,
        notes: null, passages: null, embedded: null, semantic: "off", state_dir: "/test/state", error: null,
      },
      clients: { clients: [], connections: [], error: null },
      sync: {
        configured: true, remote: null, machine: null, sync_id: null, last_success: null, last_attempt: null,
        git: "ok", index: "never", last_error: null, blocked_reason: null, conflicts: 0, held_secrets: 1,
        held_paths: ["drafts/local.env"], scheduler: "absent", interval_seconds: null, peers: [], error: null,
      },
      syncing: false, refreshed_at: "",
    }} />);

  expect(output).toMatch(/<button[^>]*class="[^"]*attention-file[^"]*"[^>]*>[^<]*(?:<span[^>]*>[^<]*<\/span>)*<span[^>]*>drafts\/local\.env<\/span><\/button>/u);
});
