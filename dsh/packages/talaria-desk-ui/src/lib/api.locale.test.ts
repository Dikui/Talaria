import { afterEach, expect, it, vi } from "vitest";
import { api } from "./api";
import { setLocale } from "./i18n";

afterEach(() => { setLocale("zh"); vi.unstubAllGlobals(); });

it("explains a service error in English without discarding the original diagnostic", async () => {
  const message = "写操作需要有效的 Bearer token";
  vi.stubGlobal("fetch", async () => Response.json({ code: "UNAUTHORIZED", message }, { status: 401 }));
  setLocale("en");
  await expect(api.health()).rejects.toMatchObject({ code: "UNAUTHORIZED", status: 401,
    message: expect.stringContaining("Refresh the workbench"), });
  await expect(api.health()).rejects.toThrow(message);
  setLocale("zh");
  await expect(api.health()).rejects.toMatchObject({ message });
});

it("preserves an already readable backend error in English", async () => {
  vi.stubGlobal("fetch", async () => Response.json({ code: "VALIDATION_ERROR", message: "body.price: invalid number" }, { status: 422 }));
  setLocale("en-US");
  await expect(api.health()).rejects.toMatchObject({ message: "body.price: invalid number" });
});
