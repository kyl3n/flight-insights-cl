import { describe, expect, it } from "vitest";
import type { SetupField } from "./setup";
import {
  entriesChanged,
  entriesOf,
  needsReason,
  parsePart,
  parseRows,
  reasonKey,
  rowOf,
  specOf,
  type StationView,
} from "./stations";

const f = (id: string, type: SetupField["type"], extra: Partial<SetupField> = {}): SetupField => ({
  id,
  label: { en: id, uk: id },
  type,
  required: false,
  ...extra,
});

const radio = [
  f("radio_role", "choice", { required: true, choices: ["telemetry", "RC", "video"] }),
  f("radio_model", "text", { required: true }),
  f("radio_air_rate", "integer"),
  f("radio_ecc", "boolean"),
];

describe("station profile parts", () => {
  const profile = {
    computer: { os: { value: "Windows", source: "detected: this computer" } },
    radios: [{ radio_role: { value: "telemetry", source: "entered" }, radio_model: { value: "RFD900x", source: "entered" } }],
  };

  it("reads single parts and lists from the profile", () => {
    expect(specOf(profile, "computer").os?.value).toBe("Windows");
    expect(specOf(profile, "radios")).toEqual({});
    expect(entriesOf(profile, "radios")).toHaveLength(1);
    expect(entriesOf(profile, "clock")).toEqual([]);
    expect(rowOf(radio, entriesOf(profile, "radios")[0]!)).toEqual({
      radio_role: "telemetry",
      radio_model: "RFD900x",
      radio_air_rate: "",
      radio_ecc: false,
    });
  });

  it("saves only the boxes a person touched", () => {
    const fields = [f("baud", "integer"), f("serial_port", "text")];
    expect(parsePart(fields, { baud: "57600" })).toEqual({ values: { baud: 57600 }, errors: {} });
    expect(parsePart(fields, { baud: "" })).toEqual({ values: { baud: null }, errors: {} }); // clears
    expect(parsePart(fields, { baud: "fast" })).toEqual({ values: {}, errors: { baud: "setup.error.integer" } });
  });
});

describe("radio and antenna lists", () => {
  it("drops empty entries and asks for what a filled one needs", () => {
    const { entries, errors } = parseRows(radio, [
      { radio_role: "RC", radio_model: " TX16S ELRS ", radio_ecc: false },
      { radio_model: "", radio_air_rate: "" },
      { radio_air_rate: "64" },
    ]);
    expect(entries).toEqual([{ radio_role: "RC", radio_model: "TX16S ELRS" }, { radio_air_rate: 64 }]);
    expect(errors).toEqual({ "2/radio_role": "stations.error.required", "2/radio_model": "stations.error.required" });
  });

  it("refuses values that do not fit", () => {
    const { errors } = parseRows(radio, [{ radio_role: "telemetry", radio_model: "SiK", radio_air_rate: "64k" }]);
    expect(errors).toEqual({ "0/radio_air_rate": "setup.error.integer" });
  });

  it("tells a changed list from the stored one", () => {
    const stored = [{ radio_role: { value: "RC", source: "entered" }, radio_model: { value: "TX16S", source: "entered" } }];
    expect(entriesChanged([{ radio_model: "TX16S", radio_role: "RC" }], stored)).toBe(false); // order of keys
    expect(entriesChanged([{ radio_role: "RC", radio_model: "TX16S", radio_ecc: false }], stored)).toBe(false);
    expect(entriesChanged([{ radio_role: "RC", radio_model: "TX15" }], stored)).toBe(true);
    expect(entriesChanged([], stored)).toBe(true);
  });
});

describe("versions", () => {
  const view = (in_use: boolean): StationView => ({
    id: "s",
    name: "Field laptop",
    version: 2,
    current: {
      id: "v", number: 1, version: 1, reason_kind: "setup", reason: "set up", effective_date: "2026-10-21",
      in_use, computer_known: false, profile: {},
    },
    history: [],
  });

  it("asks for a reason only once the version has flown", () => {
    expect(needsReason(view(false))).toBe(false);
    expect(needsReason(view(true))).toBe(true);
  });

  it("names every reason kind", () => {
    for (const kind of ["setup", "failure", "upgrade", "test", "detected"]) expect(reasonKey(kind)).not.toBeNull();
    expect(reasonKey("other")).toBeNull();
  });
});
