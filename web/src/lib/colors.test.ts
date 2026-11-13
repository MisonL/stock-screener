import { expect, test } from "vitest";
import { changeColor } from "./colors";

test("a-share red up", () => expect(changeColor(1.2, "cn_main")).toBe("--up"));
test("green down", () => expect(changeColor(-0.5, "cn_main")).toBe("--down"));
test("flat", () => expect(changeColor(0, "cn_main")).toBe("--flat"));
test("hk/us keep chinese convention", () => {
  expect(changeColor(2, "hk")).toBe("--up");
  expect(changeColor(-2, "us")).toBe("--down");
});
