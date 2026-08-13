import test from "node:test";
import assert from "node:assert/strict";
import {createHash} from "node:crypto";

const sensitive = ["token", "api_key", "apikey", "secret", "password", "passwd", "authorization", "cookie", "credential", "private_key", "access_key"];

function redact(value) {
  if (Array.isArray(value)) return value.map(redact);
  if (typeof value !== "object" || value === null) return value;
  const output = {};
  for (const [key, child] of Object.entries(value)) {
    output[key] = sensitive.some((needle) => key.toLowerCase().includes(needle)) ? "[REDACTED]" : redact(child);
  }
  return output;
}

function stableStringify(value) {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (typeof value === "object" && value !== null) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

test("recursive redaction removes sensitive values", () => {
  assert.deepEqual(redact({token: "abc", nested: {password: "x", ok: 1}}), {
    token: "[REDACTED]",
    nested: {password: "[REDACTED]", ok: 1}
  });
});

test("stable digest ignores key order", () => {
  const a = createHash("sha256").update(stableStringify({b: 1, a: 2})).digest("hex");
  const b = createHash("sha256").update(stableStringify({a: 2, b: 1})).digest("hex");
  assert.equal(a, b);
});
