/**
 * Behavior contract for the PVOutput reporting Home card
 * (web/src/views/home/PvOutputCard.tsx): the four honest states (not
 * commissioned / disabled / posting / failing), the guarded toggle's
 * typed-confirmation flow with optimistic adoption, and the quiet
 * no-surface feature detection.
 *
 * The suite drives the card with the client it is handed -- a scripted fake
 * answering `getPvOutputStatus` / `postPvOutput`; rejections carry the
 * pinned client's TypedError shape (`ApiClientError`).
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { ApiClientError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import { PvOutputCard } from "./PvOutputCard";

function refusal(code: string, message: string, status = 409): ApiClientError {
  return new ApiClientError({ code, message, details: null, request_id: "req-test", status });
}

function fakeClient(
  statusBody: () => Record<string, unknown> | Promise<Record<string, unknown>>,
  options: { post?: () => Promise<Record<string, unknown>> } = {},
): ApiClient & { postCalls: string[] } {
  const postCalls: string[] = [];
  const client = {
    getPvOutputStatus: () => Promise.resolve(statusBody()).then((body) => structuredClone(body)),
    postPvOutput: (action: string) => {
      postCalls.push(action);
      return options.post ? options.post() : Promise.resolve({});
    },
  } as unknown as ApiClient;
  return Object.assign(client, { postCalls });
}

function wireStatus(spec: Partial<Record<string, unknown>> = {}): Record<string, unknown> {
  return {
    feature: "pvoutput",
    enabled: false,
    enabled_origin: "config",
    disabled_reason: null,
    credentials_note: null,
    interval_s: 300,
    unit_slots: { lhs: ["v7", "v8"], rhs: ["v9", "v10"], mid: ["v11", "v12"] },
    native_battery_fields: true,
    as_of: "2026-08-27T01:31:00+00:00",
    last_success_at: null,
    last_post_age_s: null,
    last_posted_slot: null,
    last_error: null,
    consecutive_failures: 0,
    rate_remaining: null,
    slots_skipped_stale: 0,
    ...spec,
  };
}

afterEach(cleanup);

describe("the not-commissioned state", () => {
  it("renders the honest sentence from the structured 409, with no toggle", async () => {
    const client = {
      getPvOutputStatus: () => Promise.reject(refusal("pvoutput_not_commissioned", "no block")),
      postPvOutput: () => Promise.resolve({}),
    } as unknown as ApiClient;
    render(<PvOutputCard client={client} />);
    const sentence = await screen.findByText(/not commissioned on this controller/i);
    expect(sentence).toBeVisible();
    expect(screen.queryByRole("switch")).toBeNull();
  });
});

describe("the composed states", () => {
  it("renders the disabled state with the config-origin phrase", async () => {
    render(<PvOutputCard client={fakeClient(() => wireStatus())} />);
    expect(await screen.findByText(/Standing by/i)).toBeVisible();
    expect(screen.getByText(/Off \(config\)/)).toBeVisible();
    const toggle = screen.getByRole("switch", { name: "PVOutput reporting" });
    expect(toggle.getAttribute("aria-checked")).toBe("false");
  });

  it("renders the posting state with the last post's facts", async () => {
    render(
      <PvOutputCard
        client={fakeClient(() =>
          wireStatus({
            enabled: true,
            enabled_origin: "runtime",
            last_success_at: "2026-08-26T15:30:01+00:00",
            last_post_age_s: 112,
            last_posted_slot: "2026-08-27 01:30",
            rate_remaining: 43,
          }),
        )}
      />,
    );
    expect(await screen.findByText(/Last post 1 min ago/i)).toBeVisible();
    expect(screen.getByText(/slot 2026-08-27 01:30/)).toBeVisible();
    expect(screen.getByText(/43 posts left this hour/)).toBeVisible();
    expect(screen.getByText(/On — kept across restarts/)).toBeVisible();
  });

  it("renders the failing state loudly, with PVOutput's own words", async () => {
    render(
      <PvOutputCard
        client={fakeClient(() =>
          wireStatus({
            enabled: true,
            disabled_reason: "auth_failed",
            last_error: "Read only key",
            consecutive_failures: 3,
          }),
        )}
      />,
    );
    const status = await screen.findByText(/refused the credentials/i);
    expect(status).toBeVisible();
    expect(status.textContent).toContain("Read only key");
  });

  it("names an honestly-skipped slot when one exists", async () => {
    render(
      <PvOutputCard client={fakeClient(() => wireStatus({ slots_skipped_stale: 2 }))} />,
    );
    expect(await screen.findByText(/2 slots skipped/i)).toBeVisible();
  });

  it("stays quiet about gaps at zero", async () => {
    render(<PvOutputCard client={fakeClient(() => wireStatus())} />);
    await screen.findByText(/Standing by/i);
    expect(screen.queryByText(/skipped/i)).toBeNull();
  });

  it("says the state could not be read on a non-refusal failure, without crashing", async () => {
    const client = {
      getPvOutputStatus: () => Promise.reject(refusal("network_error", "unreachable", 0)),
      postPvOutput: () => Promise.resolve({}),
    } as unknown as ApiClient;
    render(<PvOutputCard client={client} />);
    expect(await screen.findByText(/could not be read/i)).toBeVisible();
  });

  it("renders nothing at all for a client without the surface", () => {
    const { container } = render(<PvOutputCard client={{} as unknown as ApiClient} />);
    expect(container.textContent).toBe("");
  });
});

describe("the guarded toggle", () => {
  async function openDialog(action: "enable" | "disable") {
    const client = fakeClient(() =>
      wireStatus(action === "enable" ? {} : { enabled: true, enabled_origin: "runtime" }),
    );
    render(<PvOutputCard client={client} />);
    await screen.findByText(action === "enable" ? /Standing by/i : /Posting every 5 min/i);
    await userEvent.click(screen.getByRole("switch", { name: "PVOutput reporting" }));
    const confirm = await screen.findByRole("button", {
      name: action === "enable" ? "Turn on" : "Turn off",
    });
    return { client, confirm };
  }

  it("demands the typed confirmation before the mutation", async () => {
    const { client, confirm } = await openDialog("enable");
    expect(confirm.hasAttribute("disabled")).toBe(true);
    const field = screen.getByLabelText(/Type PVOUTPUT to confirm turning it on/i);
    await userEvent.type(field, "PVOUTPT");
    expect(confirm.hasAttribute("disabled")).toBe(true);
    expect(client.postCalls).toEqual([]);
    await userEvent.clear(field);
    await userEvent.type(field, "PVOUTPUT");
    expect(confirm.hasAttribute("disabled")).toBe(false);
  });

  it("adopts the 200's own post-toggle state optimistically", async () => {
    const client = fakeClient(() => wireStatus(), {
      post: () =>
        Promise.resolve({
          feature: "pvoutput",
          enabled: true,
          enabled_origin: "runtime",
          persisted: true,
          pvoutput_state: wireStatus({ enabled: true, enabled_origin: "runtime" }),
        }),
    });
    render(<PvOutputCard client={client} />);
    await screen.findByText(/Standing by/i);
    await userEvent.click(screen.getByRole("switch", { name: "PVOutput reporting" }));
    await userEvent.type(
      screen.getByLabelText(/Type PVOUTPUT to confirm turning it on/i),
      "PVOUTPUT",
    );
    await userEvent.click(screen.getByRole("button", { name: "Turn on" }));
    expect(client.postCalls).toEqual(["enable"]);
    await waitFor(() => {
      expect(screen.getByText(/On — kept across restarts/)).toBeVisible();
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps the dialog open and renders the refusal envelope inline", async () => {
    const client = fakeClient(() => wireStatus(), {
      post: () =>
        Promise.reject(refusal("pvoutput_toggle_failed", "the store refused the write")),
    });
    render(<PvOutputCard client={client} />);
    await screen.findByText(/Standing by/i);
    await userEvent.click(screen.getByRole("switch", { name: "PVOutput reporting" }));
    await userEvent.type(
      screen.getByLabelText(/Type PVOUTPUT to confirm turning it on/i),
      "PVOUTPUT",
    );
    await userEvent.click(screen.getByRole("button", { name: "Turn on" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("pvoutput_toggle_failed");
    expect(alert.textContent).toContain("nothing changed");
    expect(screen.getByRole("dialog")).toBeVisible();
  });

  it("closes on Escape and hands focus back to the switch", async () => {
    const { confirm } = await openDialog("enable");
    expect(confirm).toBeVisible();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("switch", { name: "PVOutput reporting" })).toHaveFocus();
  });

  it("disables from the posting state with the same typed confirmation", async () => {
    const client = fakeClient(() => wireStatus({ enabled: true, enabled_origin: "runtime" }), {
      post: () =>
        Promise.resolve({
          pvoutput_state: wireStatus({ enabled: false, enabled_origin: "runtime" }),
        }),
    });
    render(<PvOutputCard client={client} />);
    await screen.findByText(/On — kept across restarts/);
    await userEvent.click(screen.getByRole("switch", { name: "PVOutput reporting" }));
    await userEvent.type(
      screen.getByLabelText(/Type PVOUTPUT to confirm turning it off/i),
      "PVOUTPUT",
    );
    await userEvent.click(screen.getByRole("button", { name: "Turn off" }));
    expect(client.postCalls).toEqual(["disable"]);
    await waitFor(() => {
      expect(screen.getByText(/Off — kept across restarts/)).toBeVisible();
    });
  });
});
