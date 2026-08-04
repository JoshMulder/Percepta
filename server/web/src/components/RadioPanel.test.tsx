import { describe, expect, it } from "vitest";
import { formatFreqEntry } from "./RadioPanel";

describe("formatFreqEntry", () => {
  it("inserts the point after the third digit as you type", () => {
    expect(formatFreqEntry("128")).toBe("128");
    expect(formatFreqEntry("1289")).toBe("128.9");
    expect(formatFreqEntry("128950")).toBe("128.950");
  });

  it("leaves a point the operator typed themselves where it belongs", () => {
    expect(formatFreqEntry("128.950")).toBe("128.950");
    expect(formatFreqEntry("128.9")).toBe("128.9");
  });

  it("stops at six digits — the airband frequency is xxx.xxx", () => {
    expect(formatFreqEntry("1289501234")).toBe("128.950");
  });

  it("ignores stray non-digits rather than showing them", () => {
    expect(formatFreqEntry("1a2b8c9")).toBe("128.9");
  });
});
