import { describe, expect, it } from "vitest";
import { look, ordered, outages, shortSet, wide, type CardSection, type RecoveryCard } from "./recovery";

const text = (en: string) => ({ en, uk: en });
const phrase = (key: string) => ({ key, text: text(key) });
const section = (id: string, alert = false, lines = 1): CardSection => ({
  id,
  title: text(id),
  will: Array.from({ length: lines }, (_, i) => phrase(`will.${i}`)),
  facts: [],
  steps: [phrase("step.land")],
  alert,
});
const none = { outages: 0, hold_s: null, detection_s: null, drift_m_per_min: null };
const card = (sections: CardSection[]): RecoveryCard => ({
  airframe: "HL1",
  param_set: "a3f9c2d1e5",
  title: text("HL1: recovery card"),
  valid: null,
  home_heading: text("Home heading"),
  will_label: text("The aircraft will"),
  fallback_mode: "AltHold",
  measured: { alone: { ...none, outages: 2 }, on_set: { ...none, outages: 1 }, tolerance_m: 10 },
  sections,
  gaps: [],
  footer: text("footer"),
});

describe("the recovery card", () => {
  it("draws the engine's alerts in red and spoofing apart", () => {
    expect(look(section("link_lost", true))).toBe("alert");
    expect(look(section("gnss_lost"))).toBe("section");
    expect(look(section("spoofing", true))).toBe("spoofing");
  });

  it("gives long sections the whole row", () => {
    expect(wide(section("link_lost"))).toBe(true);
    expect(wide(section("gnss_lost", false, 6))).toBe(true);
    expect(wide(section("motor_lost"))).toBe(false);
  });

  it("prints spoofing after the losses, and counts what it rests on", () => {
    const c = card([section("gnss_lost"), section("spoofing"), section("link_lost"), section("motor_lost")]);
    expect(ordered(c).map((s) => s.id)).toEqual(["gnss_lost", "link_lost", "motor_lost", "spoofing"]);
    expect(outages(c)).toBe(3);
    expect(shortSet(c.param_set)).toBe("a3f9c2d1");
    expect(shortSet(null)).toBeNull();
  });
});
