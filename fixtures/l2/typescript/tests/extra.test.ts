import { expect, test } from "vitest";
import { shout } from "../src/greeting";

// Deliberately red (issue #18): gives test_all a real, named failure to
// count and identify, and gives test_targeted something to prove it skips
// when only greeting.test.ts is named.
test("shout", () => {
  expect(shout("world")).toBe("hello, world!");
});
