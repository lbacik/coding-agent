import { expect, test } from "vitest";
import { greet } from "../src/greeting";

test("greet", () => {
  expect(greet("World")).toBe("Hello, World!");
});
