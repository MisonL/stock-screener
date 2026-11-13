import { describe, expect, test } from "vitest";
import { resolveMarket } from "./market";
import { routes } from "../routes";

const u = (p: string) => new URL(p, "http://x");
const store = (v: string | null) =>
  ({ getItem: () => v, setItem() {}, removeItem() {} }) as unknown as Storage;
const stockPage = "/stock/sz300750";
const listPage = "/";

describe("resolveMarket", () => {
  test("url beats storage", () => {
    expect(resolveMarket(u("/?m=cn_gem"), store("cn_star"), listPage)).toBe("cn_gem");
  });
  test("default main", () => {
    expect(resolveMarket(u("/"), store(null), listPage)).toBe("cn_main");
  });
  test("stock page follows stock", () => {
    expect(resolveMarket(u("/stock/sz300750?m=cn_main"), store(null), stockPage, "cn_gem")).toBe("cn_gem");
  });
  test("routes include sector and settings", () => {
    expect(routes).toEqual(expect.arrayContaining(["/sector", "/settings", "/auth"]));
  });
});

test("setMarket writes both storage and url", async () => {
  const { setMarket } = await import("./market");
  const writes: string[] = [];
  const storage = { getItem: () => null, setItem: (_k: string, v: string) => writes.push(v), removeItem() {} } as unknown as Storage;
  const url = setMarket("cn_star", new URL("http://x/quotes"), storage);
  expect(writes).toEqual(["cn_star"]);
  expect(url.searchParams.get("m")).toBe("cn_star");
});
