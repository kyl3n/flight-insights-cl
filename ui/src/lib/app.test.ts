import { describe, expect, it } from "vitest";
import { appTitle } from "./app";

describe("app", () => {
  it("is titled Flight Insights", () => {
    expect(appTitle).toBe("Flight Insights");
  });
});
