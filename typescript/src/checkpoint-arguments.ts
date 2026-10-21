// Modified for Norma.
import type { JsonValue } from "./types";

/** Copy data without invoking getters or silently dropping non-JSON values. */
export function copyCheckpointArguments<TArgs extends unknown[]>(args: TArgs, freeze: boolean): TArgs {
  const active = new Set<object>();
  const copies = new Map<object, JsonValue>();
  const invalid = (): never => {
    throw new TypeError("Checkpoint arguments must contain only finite JSON values, dense arrays, and plain data objects. Execution blocked.");
  };

  const copy = (value: unknown): JsonValue => {
    if (value === null || typeof value === "string" || typeof value === "boolean") return value;
    if (typeof value === "number") {
      if (!Number.isFinite(value) || Object.is(value, -0)) return invalid();
      return value;
    }
    if (typeof value !== "object") return invalid();
    if (active.has(value)) return invalid();
    const previous = copies.get(value);
    if (previous !== undefined) return previous;

    const array = Array.isArray(value);
    const prototype: unknown = Object.getPrototypeOf(value);
    if (array ? prototype !== Array.prototype : prototype !== Object.prototype && prototype !== null) return invalid();
    const descriptors = Object.getOwnPropertyDescriptors(value);
    const keys = Reflect.ownKeys(descriptors);
    if (keys.some((key) => typeof key !== "string")) return invalid();
    const result: JsonValue[] | { [key: string]: JsonValue } = array ? [] : Object.create(prototype === null ? null : Object.prototype);
    if (array) {
      const length: unknown = descriptors.length?.value;
      if (typeof length !== "number" || keys.length !== length + 1) return invalid();
      for (let index = 0; index < length; index++) {
        if (!Object.hasOwn(descriptors, String(index))) return invalid();
      }
    }

    active.add(value);
    copies.set(value, result);
    for (const key of keys) {
      if (typeof key !== "string" || (array && key === "length")) continue;
      const descriptor = descriptors[key];
      if (!descriptor.enumerable || !Object.hasOwn(descriptor, "value")) return invalid();
      Object.defineProperty(result, key, {
        value: copy(descriptor.value), enumerable: true, configurable: true, writable: true,
      });
    }
    active.delete(value);
    if (freeze) Object.freeze(result);
    return result;
  };

  // Validation above preserves the supplied tuple's JSON values and shared references.
  return copy(args) as TArgs;
}
