import { afterEach, expect, it, vi } from "vitest";
import { api, configureApi, initializeStandaloneApi } from "../../src/lib/api";

afterEach(() => {
  configureApi({});
  vi.unstubAllGlobals();
});

// 平台 02：首个快照请求等待浏览器收到兜底页的会话 cookie。
it("waits for a session token before sending the first API request", async () => {
  let cookie = "";
  vi.stubGlobal("document", { get cookie() { return cookie; } });
  const fetch = vi.fn(async (path: string) => {
    if (path === "/desk") {
      cookie = "talaria_token=example";
      return new Response();
    }
    return Response.json({ snapshot_id: "fixture" });
  });
  vi.stubGlobal("fetch", fetch);

  await initializeStandaloneApi();
  await api.createSnapshot("BTC-USDT-PERP", ["1m"]);

  expect(fetch.mock.calls.map(([path]) => path)).toEqual(["/desk", "/api/snapshots"]);
  expect(fetch).toHaveBeenLastCalledWith("/api/snapshots", expect.objectContaining({
    headers: expect.objectContaining({ Authorization: "Bearer example" }),
  }));
});

it("surfaces an unavailable session before mounting API consumers", async () => {
  vi.stubGlobal("document", { cookie: "" });
  vi.stubGlobal("fetch", vi.fn(async () => new Response()));
  await expect(initializeStandaloneApi()).rejects.toThrow("会话凭据");
});
