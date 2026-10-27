import { describe, expect, it } from "vitest";
import { choiceLabel, display, isDetected, parseInput, stepSummary, text, type SetupField, type SetupStep } from "./setup";

const field = (type: SetupField["type"], extra: Partial<SetupField> = {}): SetupField => ({
  id: "f",
  label: { en: "Field", uk: "Поле" },
  type,
  required: false,
  ...extra,
});

describe("setup input", () => {
  it("parses what people type into the stored value", () => {
    expect(parseInput(field("number"), " 28.5 ")).toEqual({ ok: true, value: 28.5 });
    expect(parseInput(field("number"), "28,5")).toEqual({ ok: true, value: 28.5 }); // decimal comma
    expect(parseInput(field("integer"), "8")).toEqual({ ok: true, value: 8 });
    expect(parseInput(field("boolean"), true)).toEqual({ ok: true, value: true });
    expect(parseInput(field("choice", { choices: ["LiPo", "LiHV"] }), "LiHV")).toEqual({ ok: true, value: "LiHV" });
    expect(parseInput(field("text"), "  Cube Orange ")).toEqual({ ok: true, value: "Cube Orange" });
    expect(parseInput(field("number"), "")).toEqual({ ok: true, value: null }); // clears
  });

  it("explains what it refuses", () => {
    expect(parseInput(field("number"), "28 in")).toEqual({ ok: false, error: "setup.error.number" });
    expect(parseInput(field("integer"), "7.5")).toEqual({ ok: false, error: "setup.error.integer" });
    expect(parseInput(field("choice", { choices: ["LiPo"] }), "NiMH")).toEqual({ ok: false, error: "setup.error.choice" });
  });

  it("shows stored answers and where they came from", () => {
    expect(display({ value: 28, source: "entered" })).toBe("28");
    expect(display(undefined)).toBe("");
    expect(isDetected({ value: 4, source: "detected: FRAME_CLASS Quad" })).toBe(true);
    expect(isDetected({ value: 4, source: "entered" })).toBe(false);
  });
});

describe("setup steps", () => {
  const step: SetupStep = {
    id: "propulsion",
    title: { en: "Propulsion", uk: "Силова установка" },
    about: { en: "", uk: "" },
    fields: [field("number", { id: "a", required: true }), field("number", { id: "b", required: true }), field("text", { id: "c" })],
  };

  it("says how many required answers are left", () => {
    expect(stepSummary(step, "started", { a: { value: 1, source: "entered" } })).toEqual({ key: "setup.step.left", count: 1 });
    expect(stepSummary(step, "done", {})).toEqual({ key: "setup.step.done" });
    expect(stepSummary(step, "not started", {})).toEqual({ key: "setup.step.notStarted" });
    expect(stepSummary(step, "optional", {})).toEqual({ key: "setup.step.optional" });
  });

  it("reads schema text in the chosen language", () => {
    expect(text(step.title, "uk")).toBe("Силова установка");
    expect(text(undefined, "en")).toBe("");
  });
});

describe("choiceLabel", () => {
  const linkLoss: SetupField = {
    id: "link_loss",
    label: { en: "When the radio link and GNSS are lost", uk: "Коли втрачено радіозв'язок і GNSS" },
    type: "choice",
    required: false,
    choices: ["win_link_back", "land_when_unsure"],
    choice_labels: { win_link_back: { en: "Win the link back", uk: "Відновити зв'язок" } },
  };

  it("reads a coded choice in the reader's language, and any other as it is", () => {
    expect(choiceLabel(linkLoss, "win_link_back", "uk")).toBe("Відновити зв'язок");
    expect(choiceLabel(linkLoss, "land_when_unsure", "en")).toBe("land_when_unsure");
    expect(choiceLabel({ ...linkLoss, choice_labels: undefined }, "SiK", "en")).toBe("SiK");
  });
});
