// Modified for Norma.
import type { JsonObject, JsonValue } from "./types";

const MAX_BYTES = 65_536;
const MAX_DEPTH = 32;
const MAX_NODES = 10_000;
const encoder = new TextEncoder();

/**
 * Copy an explicitly supplied JSON object without running getters or toJSON.
 * Limits cover the serialized UTF-8 snapshot, including keys and punctuation.
 */
export function copyCheckpointState(value: unknown): JsonObject {
  let bytes = 0;
  let nodes = 0;
  const active = new Set<object>();
  const invalid = (): never => {
    throw new TypeError("Checkpoint state must be a plain JSON object with finite numbers and safe integers, no accessors or cycles, at most 32 levels, 10000 nodes and 65536 UTF-8 bytes.");
  };
  const count = (size: number): void => {
    bytes += size;
    if (bytes > MAX_BYTES) invalid();
  };
  const stringBytes = (text: string): void => {
    if (text.length > MAX_BYTES) invalid();
    count(encoder.encode(JSON.stringify(text)).byteLength);
  };
  const copy = (item: unknown, depth: number): JsonValue => {
    nodes++;
    if (depth > MAX_DEPTH || nodes > MAX_NODES) return invalid();
    if (item === null) { count(4); return null; }
    if (typeof item === "string") { stringBytes(item); return item; }
    if (typeof item === "boolean") { count(item ? 4 : 5); return item; }
    if (typeof item === "number") {
      if (!Number.isFinite(item) || Object.is(item, -0) || (Number.isInteger(item) && !Number.isSafeInteger(item))) return invalid();
      count(String(item).length);
      return item;
    }
    if (typeof item !== "object" || active.has(item)) return invalid();
    const array = Array.isArray(item);
    const prototype: unknown = Object.getPrototypeOf(item);
    if (array ? prototype !== Array.prototype : prototype !== Object.prototype && prototype !== null) return invalid();
    const descriptors = Object.getOwnPropertyDescriptors(item);
    const keys = Reflect.ownKeys(descriptors);
    if (keys.some(key => typeof key !== "string")) return invalid();
    const properties = keys.filter(key => key !== "length" || !array) as string[];
    // Every property consumes at least one byte. Bound traversal before copying.
    if (properties.length > MAX_NODES) return invalid();
    if (array) {
      if (descriptors.length?.value !== properties.length) return invalid();
      for (let index = 0; index < properties.length; index++) {
        if (!Object.hasOwn(descriptors, String(index))) return invalid();
      }
    }
    count(2 + Math.max(0, properties.length - 1));
    active.add(item);
    const result: JsonValue[] | JsonObject = array ? [] : {};
    for (const key of properties) {
      const descriptor = descriptors[key];
      if (!descriptor.enumerable || !Object.hasOwn(descriptor, "value")) return invalid();
      if (!array) {
        nodes++;
        if (nodes > MAX_NODES) return invalid();
        stringBytes(key); count(1);
      }
      Object.defineProperty(result, key, {
        value: copy(descriptor.value, depth + 1),
        enumerable: true, configurable: false, writable: false,
      });
    }
    active.delete(item);
    Object.freeze(result);
    return result;
  };
  if (value === null || typeof value !== "object" || Array.isArray(value)) return invalid();
  return copy(value, 0) as JsonObject;
}
